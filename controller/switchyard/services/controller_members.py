"""The controller's own cluster members: who they are, where they run, what Patroni says.

A member is a controller VM. Members on Proxmox are normally built by OpenTofu and adopted; the
interim third member on ESXi is *external* (outside the ``management`` tenant, tagged
``nofailover``). The UI lists them all with the state Patroni reports, so an administrator sees at a
glance which member leads, which one never can, and whether the external one keeps up. System
administrators manage the list (permission ``manage controller``). Reading Patroni never changes
anything.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select

from switchyard.adapters.patroni import HttpPatroni, Patroni, PatroniError
from switchyard.db import audit_log
from switchyard.db.controller_models import ControllerMemberRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.controller_members import (
    MAX_MEMBERS,
    PatroniState,
    check_member,
    is_external,
    parse_cluster,
)
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import (
    AlreadyExistsError,
    ConflictError,
    FieldErrors,
    NotFoundError,
)


@dataclass(frozen=True)
class MemberStatus:
    """What Patroni reports about a member (all ``None`` when it could not be read)."""

    database_role: str | None = None
    database_state: str | None = None
    lag_bytes: int | None = None
    timeline: int | None = None
    nofailover: bool | None = None


@dataclass(frozen=True)
class MemberView:
    """A member as the API shows it."""

    id: UUID
    name: str
    address: str
    platform: str
    host: str | None
    external: bool
    nofailover: bool
    patroni_url: str | None
    created_by: str
    created_at: datetime
    version: int
    status: MemberStatus
    in_cluster: bool | None


@dataclass(frozen=True)
class MembersView:
    """The members with the cluster view they were matched against."""

    members: list[MemberView]
    cluster_reachable: bool | None
    cluster_error: str | None
    leader: str | None


def _status(state: PatroniState | None) -> MemberStatus:
    if state is None:
        return MemberStatus()
    return MemberStatus(state.role, state.state, state.lag_bytes, state.timeline, state.nofailover)


def _view(
    row: ControllerMemberRow, state: PatroniState | None, in_cluster: bool | None
) -> MemberView:
    return MemberView(
        id=row.id,
        name=row.name,
        address=row.address,
        platform=row.platform,
        host=row.host,
        external=row.external,
        nofailover=row.nofailover,
        patroni_url=row.patroni_url,
        created_by=row.created_by,
        created_at=row.created_at,
        version=row.version,
        status=_status(state),
        in_cluster=in_cluster,
    )


class ControllerMembers:
    """List, add, change and remove controller cluster members."""

    def __init__(self, db: Databases, patroni: Patroni | None = None) -> None:
        """Create the service; ``patroni`` is the REST client (a fake in tests)."""
        self._db = db
        self._patroni = patroni or HttpPatroni()

    async def list_members(self, principal: Principal) -> MembersView:
        """Every member, matched with Patroni's cluster view.

        The view comes from the first member whose ``patroni_url`` answers. A member that is not in
        that view is shown with ``in_cluster: false``; with no Patroni URL to ask at all,
        ``cluster_reachable`` is ``None`` and no member has a state.
        """
        require(principal, "manage", "controller")
        async with admin_session(self._db.admin) as session:
            rows = list(
                (
                    await session.execute(
                        select(ControllerMemberRow).order_by(
                            ControllerMemberRow.external, ControllerMemberRow.name
                        )
                    )
                ).scalars()
            )
        urls = [r.patroni_url for r in rows if r.patroni_url]
        cluster: dict[str, PatroniState] | None = None
        error: str | None = None
        for url, answer in zip(
            urls, await asyncio.gather(*(self._ask(u) for u in urls)), strict=True
        ):
            if isinstance(answer, str):
                error = f"{url}: {answer}"
            elif cluster is None:
                cluster = answer
        reachable = None if not urls else cluster is not None
        leader = next(
            (n for n, s in (cluster or {}).items() if s.role in {"leader", "standby_leader"}),
            None,
        )
        views = [
            _view(
                r,
                (cluster or {}).get(r.name),
                None if cluster is None else r.name in cluster,
            )
            for r in rows
        ]
        return MembersView(views, reachable, None if reachable in (None, True) else error, leader)

    async def _ask(self, url: str) -> dict[str, PatroniState] | str:
        """One member's cluster view, or the reason it could not be read."""
        try:
            return parse_cluster(await self._patroni.cluster(url))
        except PatroniError as exc:
            return str(exc)

    async def create(
        self,
        principal: Principal,
        *,
        name: str,
        address: str,
        platform: str,
        host: str | None = None,
        external: bool | None = None,
        nofailover: bool = False,
        patroni_url: str | None = None,
        request_id: str | None = None,
    ) -> MemberView:
        """Add a member.

        ``external`` defaults to true for every platform but Proxmox. Raises :class:`FieldErrors`
        for invalid fields and :class:`AlreadyExistsError` for a taken name.
        """
        require(principal, "manage", "controller")
        errors = FieldErrors()
        for path, message in check_member(name, address, platform, patroni_url, host):
            errors.add(path, "invalid", message)
        errors.raise_if_any()
        async with admin_session(self._db.admin) as session:
            if (
                await session.execute(
                    select(ControllerMemberRow.id).where(ControllerMemberRow.name == name)
                )
            ).first():
                raise AlreadyExistsError(f"controller member {name} already exists")
            count = (
                await session.execute(select(func.count()).select_from(ControllerMemberRow))
            ).scalar_one()
            if count >= MAX_MEMBERS:
                raise ConflictError(f"a cluster lists at most {MAX_MEMBERS} members")
            row = ControllerMemberRow(
                id=uuid7(),
                name=name,
                address=address,
                platform=platform,
                host=host,
                external=is_external(platform) if external is None else external,
                nofailover=nofailover,
                patroni_url=patroni_url.rstrip("/") if patroni_url else None,
                created_by=principal.actor,
            )
            session.add(row)
            await session.flush()
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="controller_member.created",
                object_ref=f"controller_member:{name}",
                diff={"platform": platform, "external": row.external},
                request_id=request_id,
            )
            return _view(row, None, None)

    async def update(
        self,
        principal: Principal,
        member: UUID,
        *,
        expected_version: int,
        changes: dict[str, object],
        request_id: str | None = None,
    ) -> MemberView:
        """Change a member; ``changes`` holds only the fields to change (``name`` is fixed)."""
        require(principal, "manage", "controller")
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(ControllerMemberRow)
                    .where(ControllerMemberRow.id == member)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("controller member not found")
            if row.version != expected_version:
                raise ConflictError(f"member is at version {row.version}, not {expected_version}")
            merged = {
                "address": row.address,
                "platform": row.platform,
                "patroni_url": row.patroni_url,
                "host": row.host,
            } | {
                k: v
                for k, v in changes.items()
                if k in {"address", "platform", "patroni_url", "host"}
            }
            errors = FieldErrors()
            for path, message in check_member(
                row.name,
                str(merged["address"]),
                str(merged["platform"]),
                merged["patroni_url"],  # type: ignore[arg-type]
                merged["host"],  # type: ignore[arg-type]
            ):
                errors.add(path, "invalid", message)
            errors.raise_if_any()
            for key, value in changes.items():
                if key == "patroni_url" and isinstance(value, str):
                    value = value.rstrip("/")
                setattr(row, key, value)
            if "platform" in changes and "external" not in changes:
                row.external = is_external(row.platform)
            row.version += 1
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="controller_member.updated",
                object_ref=f"controller_member:{row.name}",
                diff=dict(changes),
                request_id=request_id,
            )
            return _view(row, None, None)

    async def delete(
        self,
        principal: Principal,
        member: UUID,
        *,
        expected_version: int,
        request_id: str | None = None,
    ) -> None:
        """Forget a member (this does not touch the VM, etcd or Patroni)."""
        require(principal, "manage", "controller")
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(ControllerMemberRow)
                    .where(ControllerMemberRow.id == member)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("controller member not found")
            if row.version != expected_version:
                raise ConflictError(f"member is at version {row.version}, not {expected_version}")
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="controller_member.deleted",
                object_ref=f"controller_member:{row.name}",
                request_id=request_id,
            )
            await session.delete(row)
