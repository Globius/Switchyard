"""Change sets: draft, preview, approve, apply and roll back.

This is the only write path for desired state (backend design, "Writes go through a change set").
Every method checks the caller's tenant scope and role, runs inside a tenant-scoped transaction and
writes an audit event.

Lifecycle::

    draft ──request_approval──► pending_approval ──approve──► applying ──► applied
      │  ▲                          │ reject / edit                      └──► failed
      │  └──────────────────────────┘
      ├──apply (approval not required)──► applying
      └──discard──► discarded            failed ──edit / apply again──► draft / applying

Applying only queues the ``apply_change_set`` job; the pipeline (:mod:`switchyard.pipeline.apply`)
does the work in a worker.
"""

import difflib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from switchyard.db import audit_log
from switchyard.db import changesets as store
from switchyard.db.models import TenantRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import tenant_session
from switchyard.domain.changes import (
    ChangeOp,
    Draft,
    ObjectType,
    apply_ops,
    ops_between,
)
from switchyard.domain.errors import Problem
from switchyard.domain.snapshot import Snapshot
from switchyard.domain.workflow import ChangeSetState, Role
from switchyard.ids import uuid7
from switchyard.jobs import queue
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import RenderResult, render_haproxy
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import ConflictError, NotFoundError, PermissionDeniedError

_ROLE_RANK = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.TENANT_ADMIN: 2, Role.SYSTEM_ADMIN: 3}
#: Object types that only tenant administrators may change (infrastructure, not applications).
INFRASTRUCTURE = frozenset({ObjectType.NETWORK, ObjectType.VIP_POOL, ObjectType.SE_GROUP})

APPLY_JOB = "apply_change_set"


class ValidationFailedError(ValueError):
    """The proposed state breaks domain rules; ``problems`` lists every finding."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        """Wrap the findings."""
        self.problems = list(problems)
        super().__init__("; ".join(f"{p.path}: {p.message}" for p in self.problems))


@dataclass(frozen=True)
class FileDiff:
    """Difference of one rendered file."""

    path: str
    status: str  # added | removed | modified
    diff: str


@dataclass(frozen=True)
class GroupDiff:
    """Rendered configuration difference for one SE group.

    Attributes:
        se_group_id: The group.
        name: Its name.
        files: Changed files with unified diffs (empty when nothing changes).
        error: Why rendering failed, if it did.
    """

    se_group_id: UUID
    name: str
    files: tuple[FileDiff, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ChangeSetDetail:
    """A change set with its operations."""

    record: store.ChangeSetRecord
    ops: tuple[ChangeOp, ...]


@dataclass(frozen=True)
class Preview:
    """A change set applied in memory: objects, rule findings and rendered differences."""

    detail: ChangeSetDetail
    draft: Draft
    groups: tuple[GroupDiff, ...] = field(default=())


def require_role(principal: Principal, minimum: Role) -> None:
    """Raise :class:`PermissionDeniedError` unless the principal holds at least ``minimum``."""
    if _ROLE_RANK[principal.role] < _ROLE_RANK[minimum]:
        raise PermissionDeniedError(f"{principal.actor} needs the {minimum} role")


def _require_tenant(principal: Principal, tenant_id: UUID) -> None:
    if not principal.can_access(tenant_id):
        raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")


def _check_ops_allowed(principal: Principal, ops: Sequence[ChangeOp]) -> None:
    if any(op.object_type in INFRASTRUCTURE for op in ops):
        require_role(principal, Role.TENANT_ADMIN)
    else:
        require_role(principal, Role.OPERATOR)


def fill_member_ids(op: ChangeOp) -> ChangeOp:
    """Give pool members that have no ``id`` a fresh UUIDv7, so preview and apply agree.

    Ids must be chosen when the operation is stored, not when it is applied: otherwise the preview a
    user approved and the state that gets committed would carry different member ids.
    """
    if op.object_type is not ObjectType.POOL or not op.body or "members" not in op.body:
        return op
    members = [
        {**m, "id": m.get("id") or str(uuid7())} if isinstance(m, dict) else m
        for m in op.body["members"] or []
    ]
    return op.model_copy(update={"body": {**op.body, "members": members}})


def _line_diff(path: str, before: str | None, after: str | None) -> FileDiff | None:
    if before == after:
        return None
    status = "added" if before is None else "removed" if after is None else "modified"
    diff = "".join(
        difflib.unified_diff(
            (before or "").splitlines(keepends=True),
            (after or "").splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    return FileDiff(path, status, diff)


def render_diffs(before: Snapshot, after: Snapshot) -> tuple[GroupDiff, ...]:
    """Render every SE group of ``after`` against its state in ``before`` and diff the files."""
    out: list[GroupDiff] = []
    old_groups = {g.id for g in before.se_groups}
    for group in sorted(after.se_groups, key=lambda g: g.name):
        try:
            new_files: Mapping[str, str] = render_haproxy(after, group.id).files
            old: RenderResult | None = (
                render_haproxy(before, group.id) if group.id in old_groups else None
            )
        except (RenderError, ValueError) as exc:
            out.append(GroupDiff(group.id, group.name, error=str(exc)))
            continue
        old_files: Mapping[str, str] = old.files if old else {}
        files = [
            d
            for path in sorted(set(old_files) | set(new_files))
            if (d := _line_diff(path, old_files.get(path), new_files.get(path)))
        ]
        out.append(GroupDiff(group.id, group.name, tuple(files)))
    return tuple(out)


class ChangeSetService:
    """Creates, edits, previews, approves, applies and rolls back change sets."""

    def __init__(self, db: Databases) -> None:
        """Create the service over the application and admin databases."""
        self._db = db

    # -- reading ---------------------------------------------------------------------------------

    async def get(
        self, principal: Principal, tenant_id: UUID, change_set_id: UUID
    ) -> ChangeSetDetail:
        """Return a change set with its operations."""
        _require_tenant(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            return await self._detail(session, change_set_id)

    async def list_change_sets(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        states: set[ChangeSetState] | None = None,
        limit: int = 50,
    ) -> list[store.ChangeSetRecord]:
        """List change sets, newest first."""
        _require_tenant(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            return await store.list_change_sets(session, states=states, limit=limit)

    async def preview(self, principal: Principal, tenant_id: UUID, change_set_id: UUID) -> Preview:
        """Apply the change set in memory and render the result.

        Raises:
            switchyard.domain.changes.ChangeError: If an operation is invalid or stale.
        """
        _require_tenant(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            detail = await self._detail(session, change_set_id)
            base = await load_snapshot(session)
        draft = apply_ops(base, detail.ops, tenant_id=tenant_id)
        return Preview(detail, draft, render_diffs(base, draft.snapshot))

    async def draft_view(
        self, principal: Principal, tenant_id: UUID, change_set_id: UUID
    ) -> tuple[Snapshot, Draft]:
        """Return the base state and the draft, for reads with ``?change_set=``."""
        _require_tenant(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            detail = await self._detail(session, change_set_id)
            base = await load_snapshot(session)
        return base, apply_ops(base, detail.ops, tenant_id=tenant_id)

    # -- editing ---------------------------------------------------------------------------------

    async def open(
        self,
        principal: Principal,
        tenant_id: UUID,
        summary: str = "",
        *,
        request_id: str | None = None,
    ) -> ChangeSetDetail:
        """Open an empty draft."""
        _require_tenant(principal, tenant_id)
        require_role(principal, Role.OPERATOR)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            base_revision = await store.current_revision(session, tenant_id)
            record = await store.insert_change_set(
                session,
                tenant_id=tenant_id,
                author=principal.actor,
                summary=summary,
                base_revision=base_revision,
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="change_set.open",
                object_ref=f"change_set:{record.id}",
                request_id=request_id,
            )
        return ChangeSetDetail(record, ())

    async def add_ops(
        self,
        principal: Principal,
        tenant_id: UUID,
        change_set_id: UUID,
        ops: Sequence[ChangeOp],
        *,
        request_id: str | None = None,
    ) -> ChangeSetDetail:
        """Append operations after checking they apply cleanly to the current state.

        Raises:
            switchyard.domain.changes.ChangeError: If an operation is invalid, stale or unknown.
            ConflictError: If the change set can no longer be edited.
        """
        _require_tenant(principal, tenant_id)
        _check_ops_allowed(principal, ops)
        new_ops = [fill_member_ids(op) for op in ops]
        async with tenant_session(self._db.app, [tenant_id]) as session:
            record = await self._editable(session, change_set_id, principal, request_id)
            existing = await store.load_ops(session, change_set_id)
            base = await load_snapshot(session)
            apply_ops(base, [*existing, *new_ops], tenant_id=tenant_id)  # raises ChangeError
            await store.replace_ops(session, tenant_id, change_set_id, [*existing, *new_ops])
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="change_set.add_ops",
                object_ref=f"change_set:{record.id}",
                diff={"ops": [f"{o.action}:{o.object_type}:{o.object_id}" for o in new_ops]},
                request_id=request_id,
            )
            return await self._detail(session, change_set_id)

    async def remove_op(
        self,
        principal: Principal,
        tenant_id: UUID,
        change_set_id: UUID,
        position: int,
        *,
        request_id: str | None = None,
    ) -> ChangeSetDetail:
        """Remove the operation at ``position`` (0-based)."""
        _require_tenant(principal, tenant_id)
        require_role(principal, Role.OPERATOR)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            record = await self._editable(session, change_set_id, principal, request_id)
            existing = await store.load_ops(session, change_set_id)
            if not 0 <= position < len(existing):
                raise NotFoundError(f"operation {position} does not exist")
            _check_ops_allowed(principal, [existing[position]])
            remaining = [op for i, op in enumerate(existing) if i != position]
            base = await load_snapshot(session)
            apply_ops(base, remaining, tenant_id=tenant_id)
            await store.replace_ops(session, tenant_id, change_set_id, remaining)
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="change_set.remove_op",
                object_ref=f"change_set:{record.id}",
                diff={"position": position},
                request_id=request_id,
            )
            return await self._detail(session, change_set_id)

    async def discard(
        self,
        principal: Principal,
        tenant_id: UUID,
        change_set_id: UUID,
        *,
        request_id: str | None = None,
    ) -> store.ChangeSetRecord:
        """Throw a change set away. Applied and running change sets cannot be discarded."""
        _require_tenant(principal, tenant_id)
        require_role(principal, Role.OPERATOR)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            record = await self._locked(session, change_set_id)
            if record.state not in {
                ChangeSetState.DRAFT,
                ChangeSetState.PENDING_APPROVAL,
                ChangeSetState.FAILED,
            }:
                raise ConflictError(f"a {record.state} change set cannot be discarded")
            await store.set_state(
                session, change_set_id, ChangeSetState.DISCARDED, finished_at=func.now()
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="change_set.discard",
                object_ref=f"change_set:{change_set_id}",
                request_id=request_id,
            )
            return await self._record(session, change_set_id)

    # -- approval and apply ------------------------------------------------------------------------

    async def request_approval(
        self,
        principal: Principal,
        tenant_id: UUID,
        change_set_id: UUID,
        *,
        request_id: str | None = None,
    ) -> store.ChangeSetRecord:
        """Freeze a draft for review. Any later edit voids the request."""
        _require_tenant(principal, tenant_id)
        require_role(principal, Role.OPERATOR)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            record = await self._locked(session, change_set_id)
            if record.state not in {ChangeSetState.DRAFT, ChangeSetState.FAILED}:
                raise ConflictError(f"a {record.state} change set cannot be submitted")
            await self._validate_now(session, tenant_id, change_set_id)
            await store.set_state(
                session,
                change_set_id,
                ChangeSetState.PENDING_APPROVAL,
                requested_by=principal.actor,
                error=None,
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="change_set.request_approval",
                object_ref=f"change_set:{change_set_id}",
                request_id=request_id,
            )
            return await self._record(session, change_set_id)

    async def reject(
        self,
        principal: Principal,
        tenant_id: UUID,
        change_set_id: UUID,
        comment: str,
        *,
        request_id: str | None = None,
    ) -> store.ChangeSetRecord:
        """Send a pending change set back to draft with a comment."""
        _require_tenant(principal, tenant_id)
        require_role(principal, Role.TENANT_ADMIN)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            record = await self._locked(session, change_set_id)
            self._require_other_user(record, principal)
            if record.state is not ChangeSetState.PENDING_APPROVAL:
                raise ConflictError("only a pending change set can be rejected")
            await store.set_state(
                session, change_set_id, ChangeSetState.DRAFT, reject_comment=comment
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="change_set.reject",
                object_ref=f"change_set:{change_set_id}",
                diff={"comment": comment},
                request_id=request_id,
            )
            return await self._record(session, change_set_id)

    async def approve(
        self,
        principal: Principal,
        tenant_id: UUID,
        change_set_id: UUID,
        *,
        request_id: str | None = None,
    ) -> UUID:
        """Approve a pending change set and queue it. Returns the job id.

        The approver must hold the ``tenant-admin`` role and must not be the author.
        """
        _require_tenant(principal, tenant_id)
        require_role(principal, Role.TENANT_ADMIN)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            record = await self._locked(session, change_set_id)
            self._require_other_user(record, principal)
            if record.state is not ChangeSetState.PENDING_APPROVAL:
                raise ConflictError("only a pending change set can be approved")
            await self._validate_now(session, tenant_id, change_set_id)
            await store.set_state(
                session,
                change_set_id,
                ChangeSetState.APPLYING,
                approver=principal.actor,
                approved_at=func.now(),
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="change_set.approve",
                object_ref=f"change_set:{change_set_id}",
                request_id=request_id,
            )
            return await queue.enqueue(
                session,
                APPLY_JOB,
                {"tenant_id": str(tenant_id), "change_set_id": str(change_set_id)},
                tenant_id=tenant_id,
                max_attempts=3,
                request_id=request_id,
            )

    async def apply(
        self,
        principal: Principal,
        tenant_id: UUID,
        change_set_id: UUID,
        *,
        break_glass_reason: str | None = None,
        request_id: str | None = None,
    ) -> UUID:
        """Queue the change set for the apply pipeline. Returns the job id.

        When the tenant requires approval, only a system administrator may skip it ("break glass"),
        and must give a reason that is recorded.
        """
        _require_tenant(principal, tenant_id)
        require_role(principal, Role.OPERATOR)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            record = await self._locked(session, change_set_id)
            if record.state not in {ChangeSetState.DRAFT, ChangeSetState.FAILED}:
                raise ConflictError(f"a {record.state} change set cannot be applied")
            tenant_requires = bool(
                (
                    await session.execute(
                        select(TenantRow.require_approval).where(TenantRow.id == tenant_id)
                    )
                ).scalar_one()
            )
            if tenant_requires and (not principal.is_system or not break_glass_reason):
                raise PermissionDeniedError(
                    "this tenant requires approval; a system administrator may bypass it "
                    "only with a reason"
                )
            await self._validate_now(session, tenant_id, change_set_id)
            await store.set_state(
                session,
                change_set_id,
                ChangeSetState.APPLYING,
                break_glass_reason=break_glass_reason,
                error=None,
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="change_set.break_glass_apply" if tenant_requires else "change_set.apply",
                object_ref=f"change_set:{change_set_id}",
                diff={"reason": break_glass_reason} if break_glass_reason else None,
                request_id=request_id,
            )
            return await queue.enqueue(
                session,
                APPLY_JOB,
                {"tenant_id": str(tenant_id), "change_set_id": str(change_set_id)},
                tenant_id=tenant_id,
                max_attempts=3,
                request_id=request_id,
            )

    # -- revisions ---------------------------------------------------------------------------------

    async def revisions(
        self, principal: Principal, tenant_id: UUID, *, limit: int = 50
    ) -> list[store.RevisionRecord]:
        """List a tenant's revisions, newest first."""
        _require_tenant(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            return await store.list_revisions(session, tenant_id, limit=limit)

    async def revision(
        self, principal: Principal, tenant_id: UUID, number: int
    ) -> store.RevisionRecord:
        """Return one revision."""
        _require_tenant(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            record = await store.get_revision(session, tenant_id, number)
        if record is None:
            raise NotFoundError(f"revision {number} does not exist")
        return record

    async def rollback(
        self, principal: Principal, tenant_id: UUID, number: int, *, request_id: str | None = None
    ) -> ChangeSetDetail:
        """Open a change set that restores revision ``number``.

        The result is an ordinary draft: it can be previewed, approved and applied like any other,
        so a rollback is validated and canaried too (there is no fast path).
        """
        _require_tenant(principal, tenant_id)
        require_role(principal, Role.TENANT_ADMIN)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            revision = await store.get_revision(session, tenant_id, number)
            if revision is None:
                raise NotFoundError(f"revision {number} does not exist")
            target = Snapshot.model_validate(revision.snapshot)
            current = await load_snapshot(session)
            ops = ops_between(current, target)
            if not ops:
                raise ConflictError(f"the current state already equals revision {number}")
            record = await store.insert_change_set(
                session,
                tenant_id=tenant_id,
                author=principal.actor,
                summary=f"Roll back to revision {number}",
                base_revision=await store.current_revision(session, tenant_id),
            )
            apply_ops(current, ops, tenant_id=tenant_id)
            await store.replace_ops(session, tenant_id, record.id, ops)
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="change_set.rollback",
                object_ref=f"revision:{number}",
                diff={"change_set": str(record.id), "ops": len(ops)},
                request_id=request_id,
            )
            return await self._detail(session, record.id)

    # -- helpers -----------------------------------------------------------------------------------

    async def _record(self, session: Any, change_set_id: UUID) -> store.ChangeSetRecord:
        record = await store.get_change_set(session, change_set_id)
        if record is None:
            raise NotFoundError(f"change set {change_set_id} does not exist")
        return record

    async def _locked(self, session: Any, change_set_id: UUID) -> store.ChangeSetRecord:
        record = await store.get_change_set(session, change_set_id, for_update=True)
        if record is None:
            raise NotFoundError(f"change set {change_set_id} does not exist")
        return record

    async def _detail(self, session: Any, change_set_id: UUID) -> ChangeSetDetail:
        record = await self._record(session, change_set_id)
        return ChangeSetDetail(record, tuple(await store.load_ops(session, change_set_id)))

    async def _editable(
        self, session: Any, change_set_id: UUID, principal: Principal, request_id: str | None
    ) -> store.ChangeSetRecord:
        """Lock a change set for editing; editing a pending or failed one reopens it as a draft."""
        record = await self._locked(session, change_set_id)
        if record.state in {ChangeSetState.PENDING_APPROVAL, ChangeSetState.FAILED}:
            await store.set_state(session, change_set_id, ChangeSetState.DRAFT, error=None)
            if record.state is ChangeSetState.PENDING_APPROVAL:
                await audit_log.record(
                    session,
                    tenant_id=record.tenant_id,
                    actor=principal.actor,
                    action="change_set.approval_voided",
                    object_ref=f"change_set:{change_set_id}",
                    request_id=request_id,
                )
        elif record.state is not ChangeSetState.DRAFT:
            raise ConflictError(f"a {record.state} change set cannot be edited")
        return record

    async def _validate_now(self, session: Any, tenant_id: UUID, change_set_id: UUID) -> None:
        """Fail fast: the operations must apply cleanly and leave a valid state."""
        ops = await store.load_ops(session, change_set_id)
        base = await load_snapshot(session)
        draft = apply_ops(base, ops, tenant_id=tenant_id)
        if draft.problems:
            raise ValidationFailedError(draft.problems)

    @staticmethod
    def _require_other_user(record: store.ChangeSetRecord, principal: Principal) -> None:
        if record.author == principal.actor or record.requested_by == principal.actor:
            raise PermissionDeniedError("a change set cannot be approved by the user who wrote it")
