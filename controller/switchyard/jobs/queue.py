# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The job queue, kept in PostgreSQL (``SELECT ... FOR UPDATE SKIP LOCKED``).

Mechanics from the backend design ("Background jobs"):

- A worker *claims* one queued job whose ``run_after`` has passed. Locked rows are skipped, so
  workers never block each other.
- Jobs are idempotent and have an attempt limit. A failed job is queued again after an exponential
  backoff; after ``max_attempts`` it is ``dead`` and stays for inspection.
- A running job sends heartbeats. :func:`reclaim_stale` returns jobs whose worker went silent to the
  queue, so a crashed worker's job is picked up again.

All functions here use the admin engine: the queue is cross-tenant. Tenant-facing code enqueues in
its own transaction with :func:`enqueue` and reads status through the RLS-protected ``job`` rows.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from switchyard.db.ops_models import JobRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import JobState
from switchyard.ids import uuid7
from switchyard.jobs.progress import MAX_LOG_LINES, log_line


@dataclass(frozen=True)
class Job:
    """A claimed job.

    Attributes:
        id: Job id.
        kind: Handler name, for example ``apply_change_set``.
        tenant_id: Tenant the job belongs to, if any.
        payload: Handler input.
        attempts: How many times it has been claimed, including this one.
        max_attempts: Attempt limit.
        request_id: Request id of whoever enqueued it, carried into logs.
    """

    id: UUID
    kind: str
    tenant_id: UUID | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    request_id: str | None


def backoff(attempts: int, *, base_s: float = 5.0, cap_s: float = 300.0) -> timedelta:
    """Delay before attempt ``attempts + 1``: ``base * 2**(attempts - 1)``, capped.

    Example:
        >>> [backoff(n).total_seconds() for n in (1, 2, 3, 10)]
        [5.0, 10.0, 20.0, 300.0]
    """
    return timedelta(seconds=min(base_s * 2 ** max(attempts - 1, 0), cap_s))


async def enqueue(
    session: AsyncSession,
    kind: str,
    payload: dict[str, Any],
    *,
    tenant_id: UUID | None = None,
    delay: timedelta | None = None,
    max_attempts: int = 3,
    request_id: str | None = None,
) -> UUID:
    """Add a job in the caller's transaction and return its id."""
    job_id = uuid7()
    change_set_id = payload.get("change_set_id")
    row = JobRow(
        id=job_id,
        tenant_id=tenant_id,
        change_set_id=UUID(change_set_id) if change_set_id else None,
        kind=kind,
        payload=payload,
        max_attempts=max_attempts,
        request_id=request_id,
    )
    if delay is not None:
        row.run_after = func.now() + delay
    session.add(row)
    await session.flush()
    return job_id


async def claim(engine: AsyncEngine, worker_id: str, kinds: set[str] | None = None) -> Job | None:
    """Claim the oldest runnable job, or return ``None`` if there is none."""
    async with admin_session(engine) as session:
        query = (
            select(JobRow)
            .where(JobRow.state == JobState.QUEUED.value, JobRow.run_after <= func.now())
            .order_by(JobRow.run_after, JobRow.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if kinds is not None:
            query = query.where(JobRow.kind.in_(sorted(kinds)))
        row = (await session.execute(query)).scalar_one_or_none()
        if row is None:
            return None
        row.state = JobState.RUNNING.value
        row.attempts += 1
        row.locked_by = worker_id
        row.locked_at = func.now()
        row.heartbeat_at = func.now()
        if row.started_at is None:
            row.started_at = func.now()
        row.stages = []  # each attempt reports its own stages
        row.log = [*row.log, log_line("info", f"attempt {row.attempts} claimed by {worker_id}")][
            -MAX_LOG_LINES:
        ]
        await session.flush()
        return Job(
            id=row.id,
            kind=row.kind,
            tenant_id=row.tenant_id,
            payload=row.payload,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            request_id=row.request_id,
        )


async def heartbeat(engine: AsyncEngine, job_id: UUID) -> None:
    """Record that the worker is still running ``job_id``."""
    async with admin_session(engine) as session:
        await session.execute(
            update(JobRow).where(JobRow.id == job_id).values(heartbeat_at=func.now())
        )


async def succeed(engine: AsyncEngine, job_id: UUID, result: dict[str, Any] | None) -> None:
    """Mark a job finished."""
    async with admin_session(engine) as session:
        await session.execute(
            update(JobRow)
            .where(JobRow.id == job_id)
            .values(
                state=JobState.SUCCEEDED.value,
                result=result,
                finished_at=func.now(),
                locked_by=None,
                last_error=None,
            )
        )


async def fail(
    engine: AsyncEngine, job_id: UUID, error: str, *, delay: timedelta | None = None
) -> JobState:
    """Record a failed attempt: queue it again after a backoff, or mark it dead.

    Returns:
        The state the job ended up in (``queued`` or ``dead``).
    """
    async with admin_session(engine) as session:
        row = (
            await session.execute(select(JobRow).where(JobRow.id == job_id).with_for_update())
        ).scalar_one()
        row.last_error = error[:4000]
        row.locked_by = None
        row.log = [*row.log, log_line("error", f"attempt {row.attempts} failed: {error[:500]}")][
            -MAX_LOG_LINES:
        ]
        if row.attempts >= row.max_attempts:
            row.state = JobState.DEAD.value
            row.finished_at = func.now()
            return JobState.DEAD
        row.state = JobState.QUEUED.value
        row.run_after = func.now() + (delay if delay is not None else backoff(row.attempts))
        return JobState.QUEUED


async def reclaim_stale(engine: AsyncEngine, silent_for: timedelta) -> int:
    """Return running jobs whose heartbeat stopped to the queue. Returns how many were reclaimed."""
    async with admin_session(engine) as session:
        rows = (
            (
                await session.execute(
                    select(JobRow)
                    .where(
                        JobRow.state == JobState.RUNNING.value,
                        JobRow.heartbeat_at < func.now() - silent_for,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.last_error = f"worker {row.locked_by} stopped sending heartbeats"
            row.locked_by = None
            if row.attempts >= row.max_attempts:
                row.state = JobState.DEAD.value
                row.finished_at = func.now()
            else:
                row.state = JobState.QUEUED.value
                row.run_after = func.now()
        return len(rows)


async def cancel(engine: AsyncEngine, job_id: UUID) -> bool:
    """Cancel a job that has not started. Returns False if it is no longer ``queued``.

    A running job is never interrupted: its handler may be halfway through a rollout.
    """
    async with admin_session(engine) as session:
        row = (
            await session.execute(select(JobRow).where(JobRow.id == job_id).with_for_update())
        ).scalar_one_or_none()
        if row is None or row.state != JobState.QUEUED.value:
            return False
        row.state = JobState.CANCELLED.value
        row.finished_at = func.now()
        row.log = [*row.log, log_line("info", "cancelled")][-MAX_LOG_LINES:]
        return True


async def retry(engine: AsyncEngine, job_id: UUID) -> bool:
    """Queue a ``dead`` or ``failed`` job again with a fresh attempt budget.

    Returns False if the job is in another state.
    """
    async with admin_session(engine) as session:
        row = (
            await session.execute(select(JobRow).where(JobRow.id == job_id).with_for_update())
        ).scalar_one_or_none()
        if row is None or row.state not in {JobState.DEAD.value, JobState.FAILED.value}:
            return False
        row.state = JobState.QUEUED.value
        row.attempts = 0
        row.run_after = func.now()
        row.finished_at = None
        row.last_error = None
        row.log = [*row.log, log_line("info", "queued again by hand")][-MAX_LOG_LINES:]
        return True
