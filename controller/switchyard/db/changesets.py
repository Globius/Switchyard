"""Persistence for change sets, their operations, revisions and deployments.

Plain functions over an ``AsyncSession`` whose transaction and tenant scope the caller controls.
Business rules (who may do what, state transitions) live in :mod:`switchyard.services.changesets`.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from switchyard.db.ops_models import ChangeOpRow, ChangeSetRow, DeploymentRow, RevisionRow
from switchyard.domain.changes import Action, ChangeOp, ObjectType
from switchyard.domain.workflow import ChangeSetState, DeploymentState
from switchyard.ids import uuid7


@dataclass(frozen=True)
class ChangeSetRecord:
    """A change set row, without its operations."""

    id: UUID
    tenant_id: UUID
    version: int
    author: str
    summary: str
    message: str
    state: ChangeSetState
    base_revision: int
    requested_by: str | None
    approver: str | None
    approved_at: datetime | None
    reject_comment: str | None
    break_glass_reason: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True)
class RevisionRecord:
    """An immutable revision."""

    id: UUID
    tenant_id: UUID
    number: int
    change_set_id: UUID | None
    author: str
    summary: str
    message: str
    snapshot: dict[str, Any]
    git_sha: str | None
    created_at: datetime


@dataclass(frozen=True)
class DeploymentRecord:
    """The outcome of one SE group's rollout."""

    id: UUID
    tenant_id: UUID
    revision_id: UUID | None
    change_set_id: UUID | None
    se_group_id: UUID
    state: DeploymentState
    rendered_hash: str | None
    fingerprint: str | None
    error: str | None
    details: dict[str, Any]
    started_at: datetime
    finished_at: datetime | None


def _record(row: ChangeSetRow) -> ChangeSetRecord:
    return ChangeSetRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        version=row.version,
        author=row.author,
        summary=row.summary,
        message=row.message,
        state=ChangeSetState(row.state),
        base_revision=row.base_revision,
        requested_by=row.requested_by,
        approver=row.approver,
        approved_at=row.approved_at,
        reject_comment=row.reject_comment,
        break_glass_reason=row.break_glass_reason,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
    )


async def insert_change_set(
    session: AsyncSession, *, tenant_id: UUID, author: str, summary: str, base_revision: int
) -> ChangeSetRecord:
    """Create a draft change set."""
    row = ChangeSetRow(
        id=uuid7(),
        tenant_id=tenant_id,
        author=author,
        summary=summary,
        state=ChangeSetState.DRAFT.value,
        base_revision=base_revision,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return _record(row)


async def get_change_set(
    session: AsyncSession, change_set_id: UUID, *, for_update: bool = False
) -> ChangeSetRecord | None:
    """Return a change set, optionally locking its row for the rest of the transaction."""
    query = select(ChangeSetRow).where(ChangeSetRow.id == change_set_id)
    if for_update:
        query = query.with_for_update()
    row = (await session.execute(query)).scalar_one_or_none()
    return _record(row) if row else None


async def list_change_sets(
    session: AsyncSession,
    *,
    states: set[ChangeSetState] | None = None,
    limit: int = 50,
    before: UUID | None = None,
) -> list[ChangeSetRecord]:
    """Return change sets, newest first; ``before`` keeps only ids lower than it (the cursor).

    Ids are UUIDv7, so id order is creation order.
    """
    query = select(ChangeSetRow).order_by(ChangeSetRow.id.desc()).limit(limit)
    if states:
        query = query.where(ChangeSetRow.state.in_(sorted(s.value for s in states)))
    if before is not None:
        query = query.where(ChangeSetRow.id < before)
    return [_record(r) for r in (await session.execute(query)).scalars()]


async def count_ops(session: AsyncSession, change_set_ids: list[UUID]) -> dict[UUID, int]:
    """Return how many operations each change set holds (missing ids hold none)."""
    if not change_set_ids:
        return {}
    rows = await session.execute(
        select(ChangeOpRow.change_set_id, func.count())
        .where(ChangeOpRow.change_set_id.in_(change_set_ids))
        .group_by(ChangeOpRow.change_set_id)
    )
    return dict(rows.all())


async def set_state(
    session: AsyncSession, change_set_id: UUID, state: ChangeSetState, **fields: Any
) -> None:
    """Move a change set to ``state`` and set any other columns in ``fields``."""
    await session.execute(
        update(ChangeSetRow)
        .where(ChangeSetRow.id == change_set_id)
        .values(
            state=state.value,
            updated_at=func.now(),
            version=ChangeSetRow.version + 1,
            **fields,
        )
    )


async def load_ops(session: AsyncSession, change_set_id: UUID) -> list[ChangeOp]:
    """Return a change set's operations in order."""
    rows = (
        await session.execute(
            select(ChangeOpRow)
            .where(ChangeOpRow.change_set_id == change_set_id)
            .order_by(ChangeOpRow.position)
        )
    ).scalars()
    return [
        ChangeOp(
            object_type=ObjectType(r.object_type),
            object_id=r.object_id,
            action=Action(r.action),
            body=r.body,
            expected_version=r.expected_version,
        )
        for r in rows
    ]


async def replace_ops(
    session: AsyncSession, tenant_id: UUID, change_set_id: UUID, ops: list[ChangeOp]
) -> None:
    """Store ``ops`` as the change set's operations, replacing any existing ones."""
    await session.execute(delete(ChangeOpRow).where(ChangeOpRow.change_set_id == change_set_id))
    for position, op in enumerate(ops):
        session.add(
            ChangeOpRow(
                id=uuid7(),
                tenant_id=tenant_id,
                change_set_id=change_set_id,
                position=position,
                object_type=op.object_type.value,
                object_id=op.object_id,
                action=op.action.value,
                body=op.body,
                expected_version=op.expected_version,
            )
        )
    await session.flush()
    await session.execute(
        update(ChangeSetRow)
        .where(ChangeSetRow.id == change_set_id)
        .values(updated_at=func.now(), version=ChangeSetRow.version + 1)
    )


async def current_revision(session: AsyncSession, tenant_id: UUID) -> int:
    """Return the tenant's latest revision number (0 if there is none)."""
    value = (
        await session.execute(
            select(func.coalesce(func.max(RevisionRow.number), 0)).where(
                RevisionRow.tenant_id == tenant_id
            )
        )
    ).scalar_one()
    return int(value)


async def insert_revision(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    number: int,
    change_set_id: UUID | None,
    author: str,
    summary: str,
    snapshot: dict[str, Any],
    message: str = "",
) -> RevisionRecord:
    """Append a revision. A duplicate ``(tenant, number)`` raises ``IntegrityError``."""
    row = RevisionRow(
        id=uuid7(),
        tenant_id=tenant_id,
        number=number,
        change_set_id=change_set_id,
        author=author,
        summary=summary,
        message=message,
        snapshot=snapshot,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return _revision(row)


def _revision(row: RevisionRow) -> RevisionRecord:
    return RevisionRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        number=row.number,
        change_set_id=row.change_set_id,
        author=row.author,
        summary=row.summary,
        message=row.message,
        snapshot=row.snapshot,
        git_sha=row.git_sha,
        created_at=row.created_at,
    )


async def get_revision(
    session: AsyncSession, tenant_id: UUID, number: int
) -> RevisionRecord | None:
    """Return revision ``number`` of a tenant."""
    row = (
        await session.execute(
            select(RevisionRow).where(
                RevisionRow.tenant_id == tenant_id, RevisionRow.number == number
            )
        )
    ).scalar_one_or_none()
    return _revision(row) if row else None


async def list_revisions(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    limit: int = 50,
    before: int | None = None,
    object_id: UUID | None = None,
) -> list[RevisionRecord]:
    """Return a tenant's revisions, newest first; ``before`` keeps only numbers below it.

    With ``object_id`` only revisions whose change set had an operation on that object.
    """
    query = (
        select(RevisionRow)
        .where(RevisionRow.tenant_id == tenant_id)
        .order_by(RevisionRow.number.desc())
        .limit(limit)
    )
    if before is not None:
        query = query.where(RevisionRow.number < before)
    if object_id is not None:
        query = query.where(
            RevisionRow.change_set_id.in_(
                select(ChangeOpRow.change_set_id).where(ChangeOpRow.object_id == object_id)
            )
        )
    rows = (await session.execute(query)).scalars()
    return [_revision(r) for r in rows]


async def insert_deployment(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    change_set_id: UUID | None,
    revision_id: UUID | None,
    se_group_id: UUID,
    state: DeploymentState,
    rendered_hash: str | None,
    fingerprint: str | None,
    error: str | None,
    details: dict[str, Any],
    started_at: datetime,
) -> DeploymentRecord:
    """Record a rollout outcome."""
    row = DeploymentRow(
        id=uuid7(),
        tenant_id=tenant_id,
        change_set_id=change_set_id,
        revision_id=revision_id,
        se_group_id=se_group_id,
        state=state.value,
        rendered_hash=rendered_hash,
        fingerprint=fingerprint,
        error=error,
        details=details,
        started_at=started_at,
        finished_at=func.now(),
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return _deployment(row)


def _deployment(row: DeploymentRow) -> DeploymentRecord:
    return DeploymentRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        revision_id=row.revision_id,
        change_set_id=row.change_set_id,
        se_group_id=row.se_group_id,
        state=DeploymentState(row.state),
        rendered_hash=row.rendered_hash,
        fingerprint=row.fingerprint,
        error=row.error,
        details=row.details,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


async def list_deployments(
    session: AsyncSession, *, change_set_id: UUID | None = None, se_group_id: UUID | None = None
) -> list[DeploymentRecord]:
    """Return deployments, newest first, optionally filtered."""
    query = select(DeploymentRow).order_by(DeploymentRow.started_at.desc())
    if change_set_id:
        query = query.where(DeploymentRow.change_set_id == change_set_id)
    if se_group_id:
        query = query.where(DeploymentRow.se_group_id == se_group_id)
    return [_deployment(r) for r in (await session.execute(query)).scalars()]
