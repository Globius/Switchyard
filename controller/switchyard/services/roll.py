# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Rolling an SE group to a new template, one engine at a time (ADR 0205).

The state machine is :mod:`switchyard.domain.roll`; this module stores it, exposes start, pause,
resume and abort, and advances every running roll once per scheduled tick. What actually touches
the world (deploying a VM, enrolling it, reapplying, retiring) is behind :class:`RollActions`, so
the logic is tested with a fake and the real thing is the lab's job.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from switchyard.db import audit_log
from switchyard.db.ops_models import ServiceEngineRow
from switchyard.db.roll_models import EngineRollRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import roll as machine
from switchyard.domain.workflow import Role
from switchyard.ids import uuid7
from switchyard.services.changesets import require, require_role
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import ConflictError, NotFoundError, PermissionDeniedError

ADVANCE_ROLLS_JOB = "advance_rolls"


class RollActionError(Exception):
    """A command cannot be carried out; the roll pauses with this message."""


@dataclass(frozen=True)
class RollContext:
    """What an action needs to know about the roll it serves."""

    roll_id: UUID
    tenant_id: UUID
    se_group_id: UUID
    template_vmid: int


class RollActions(Protocol):
    """The side effects of a roll. `jobs` is its scratch space for job ids between ticks."""

    async def observe(
        self, ctx: RollContext, step: machine.EngineStep, jobs: dict[str, Any], now: float
    ) -> machine.Facts:
        """Look up the facts the current phase waits for."""
        ...

    async def execute(
        self, ctx: RollContext, command: machine.Command, jobs: dict[str, Any]
    ) -> None:
        """Carry out ``command``; record any job it queued in ``jobs`` for ``observe``."""
        ...


@dataclass(frozen=True)
class RollView:
    """A roll as the API shows it."""

    id: UUID
    tenant_id: UUID
    se_group_id: UUID
    status: str
    reason: str
    template_vmid: int
    target_version: str | None
    steps: list[dict[str, Any]]
    log: list[str]
    created_by: str
    created_at: datetime
    updated_at: datetime
    version: int
    window: str | None = None
    not_before: float | None = None
    soak_seconds: int = 0
    holding: bool = False


def _view(row: EngineRollRow) -> RollView:
    state = row.state
    window = machine.from_json(state).window
    window_label = None if window is None else window.label()
    return RollView(
        row.id, row.tenant_id, row.se_group_id, row.status, state.get("reason", ""),
        row.template_vmid, row.target_version, list(state["steps"]), list(state["log"]),
        row.created_by, row.created_at, row.updated_at, row.version,
        window=window_label,
        not_before=state.get("not_before"), soak_seconds=int(state.get("soak_s", 0)),
        holding=bool(state.get("holding", False)),
    )  # fmt: skip


class RollService:
    """Start, control and advance engine rolls."""

    def __init__(self, db: Databases, actions: RollActions | None = None) -> None:
        """``actions`` is required to advance rolls; the API alone can start and control them."""
        self._db = db
        self._actions = actions

    def _check(self, principal: Principal, tenant_id: UUID, verb: str) -> None:
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, verb, "se_group")

    async def start(
        self,
        principal: Principal,
        tenant_id: UUID,
        se_group_id: UUID,
        *,
        template_vmid: int,
        target_version: str | None = None,
        window: machine.Window | None = None,
        not_before: float | None = None,
        soak_s: float = 0.0,
        request_id: str | None = None,
    ) -> RollView:
        """Begin replacing every enabled engine of the group, in name order.

        Raises:
            NotFoundError: No such group, or it has no engines.
            ConflictError: A roll of this group is already running or paused.
        """
        self._check(principal, tenant_id, "deploy")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            engines = list(
                (
                    await session.execute(
                        select(ServiceEngineRow.name)
                        .where(
                            ServiceEngineRow.se_group_id == se_group_id,
                            ServiceEngineRow.state == "enabled",
                        )
                        .order_by(ServiceEngineRow.name)
                    )
                ).scalars()
            )
            if not engines:
                raise NotFoundError("the SE group does not exist or has no enabled engines")
            active = (
                await session.execute(
                    select(EngineRollRow.id).where(
                        EngineRollRow.se_group_id == se_group_id,
                        EngineRollRow.status.in_(["running", "paused"]),
                    )
                )
            ).first()
            if active:
                raise ConflictError("this SE group already has a roll in progress")
            state = machine.Roll.start(engines, window=window, not_before=not_before, soak_s=soak_s)
            row = EngineRollRow(
                id=uuid7(),
                tenant_id=tenant_id,
                se_group_id=se_group_id,
                status=state.status.value,
                template_vmid=template_vmid,
                target_version=target_version,
                state=machine.to_json(state),
                jobs={},
                created_by=principal.actor,
            )
            session.add(row)
            await session.flush()
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="roll.started",
                object_ref=f"se_group:{se_group_id}",
                diff={"engines": engines, "template_vmid": template_vmid},
                request_id=request_id,
            )
            return _view(row)

    async def list(self, principal: Principal, tenant_id: UUID) -> list[RollView]:
        """The tenant's rolls, newest first."""
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require_role(principal, Role.VIEWER)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = (
                await session.execute(select(EngineRollRow).order_by(EngineRollRow.id.desc()))
            ).scalars()
            return [_view(r) for r in rows]

    async def _row(self, session: AsyncSession, roll_id: UUID) -> EngineRollRow:
        row = (
            await session.execute(
                select(EngineRollRow).where(EngineRollRow.id == roll_id).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("roll not found")
        return row

    async def control(
        self,
        principal: Principal,
        tenant_id: UUID,
        roll_id: UUID,
        action: str,
        *,
        request_id: str | None = None,
    ) -> RollView:
        """``pause``, ``resume`` or ``abort`` a roll."""
        self._check(principal, tenant_id, "deploy")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await self._row(session, roll_id)
            state = machine.from_json(row.state)
            if row.status in ("aborted", "done"):
                raise ConflictError(f"the roll is already {row.status}")
            if action == "pause":
                state = machine.pause(state, f"paused by {principal.actor}")
            elif action == "resume":
                if row.status != "paused":
                    raise ConflictError("only a paused roll can be resumed")
                state = machine.resume(state, time.time())
            elif action == "abort":
                state = machine.abort(state)
            else:
                raise ValueError(f"unknown action {action!r}")
            row.state, row.status = machine.to_json(state), state.status.value
            row.updated_at = datetime.now(UTC)
            row.version += 1
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action=f"roll.{action}",
                object_ref=f"se_group:{row.se_group_id}",
                request_id=request_id,
            )
            return _view(row)

    async def tick(self) -> int:
        """Advance every running roll by one step. Returns how many rolls were looked at."""
        if self._actions is None:
            return 0
        async with admin_session(self._db.admin) as session:
            running = list(
                (
                    await session.execute(
                        select(EngineRollRow.id, EngineRollRow.tenant_id).where(
                            EngineRollRow.status == "running"
                        )
                    )
                ).all()
            )
        for roll_id, tenant_id in running:
            await self._advance(tenant_id, roll_id)
        return len(running)

    async def _advance(self, tenant_id: UUID, roll_id: UUID) -> None:
        assert self._actions is not None  # noqa: S101 (checked by tick)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await self._row(session, roll_id)
            if row.status != "running":
                return
            state = machine.from_json(row.state)
            ctx = RollContext(row.id, row.tenant_id, row.se_group_id, row.template_vmid)
            jobs = dict(row.jobs)
            step = state.current
            now = time.time()
            before = state.status
            try:
                facts = (
                    await self._actions.observe(ctx, step, jobs, now)
                    if step is not None
                    else machine.Facts(now=now)
                )
                state, command = machine.advance(state, facts)
                if command is not None:
                    await self._actions.execute(ctx, command, jobs)
            except RollActionError as exc:
                state = machine.pause(state, str(exc))
            row.state, row.jobs, row.status = machine.to_json(state), jobs, state.status.value
            row.updated_at = datetime.now(UTC)
            if state.status is not before:
                await audit_log.record(
                    session,
                    tenant_id=tenant_id,
                    actor="system:roll",
                    action=f"roll.{state.status.value}",
                    object_ref=f"se_group:{row.se_group_id}",
                    diff={"reason": state.reason},
                )
