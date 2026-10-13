# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reading and managing background jobs, scoped to a tenant.

Reads go through the RLS-protected application role. Retry and cancel change jobs, which the
application role may not do, so they use the admin engine after checking the role and that the job
belongs to the tenant; both are audited.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, tuple_

from switchyard.db import audit_log
from switchyard.db import changesets as store
from switchyard.db.ops_models import JobRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.workflow import ChangeSetState, JobState
from switchyard.jobs import queue
from switchyard.services.changesets import _require_tenant, require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import ConflictError, NotFoundError

APPLY_KIND = "apply_change_set"
#: Jobs an operator starts (applying a change set, issuing or renewing certificates). An operator
#: may retry or cancel these; everything else (deployments, SDN, retirement) needs a tenant admin.
OPERATOR_JOB_KINDS = frozenset({APPLY_KIND, "issue_cert", "renew_certs"})


def _require_job_role(principal: Principal, job: JobRow, verb: str) -> None:
    """Check the role needed to retry or cancel ``job``: the role of the action that created it."""
    require(principal, verb, "job_own" if job.kind in OPERATOR_JOB_KINDS else "job")


@dataclass(frozen=True)
class JobPage:
    """One page of jobs, newest first.

    Attributes:
        items: Job rows.
        next_cursor: Pass back as ``cursor`` for the next page; ``None`` on the last page.
    """

    items: list[JobRow]
    next_cursor: str | None


class JobService:
    """Tenant-scoped job queries and the retry and cancel actions."""

    def __init__(self, db: Databases) -> None:
        """Create the service."""
        self._db = db

    async def list(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        state: JobState | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> JobPage:
        """List the tenant's jobs, newest first (by creation time, then id).

        Raises:
            ValueError: If ``cursor`` is not one this method returned.
        """
        _require_tenant(principal, tenant_id)
        query = select(JobRow).where(JobRow.tenant_id == tenant_id)
        if state is not None:
            query = query.where(JobRow.state == state.value)
        if kind is not None:
            query = query.where(JobRow.kind == kind)
        if cursor:
            stamp, _, last_id = cursor.partition("|")
            query = query.where(
                tuple_(JobRow.created_at, JobRow.id)
                < tuple_(datetime.fromisoformat(stamp), UUID(last_id))
            )
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = list(
                (
                    await session.execute(
                        query.order_by(JobRow.created_at.desc(), JobRow.id.desc()).limit(limit + 1)
                    )
                )
                .scalars()
                .all()
            )
        more = len(rows) > limit
        rows = rows[:limit]
        return JobPage(rows, f"{rows[-1].created_at.isoformat()}|{rows[-1].id}" if more else None)

    async def get(self, principal: Principal, tenant_id: UUID, job_id: UUID) -> JobRow:
        """Return one of the tenant's jobs.

        Raises:
            NotFoundError: If it does not exist for this tenant.
        """
        _require_tenant(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = (
                await session.execute(
                    select(JobRow).where(JobRow.id == job_id, JobRow.tenant_id == tenant_id)
                )
            ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("job not found")
        return row

    async def retry(
        self, principal: Principal, tenant_id: UUID, job_id: UUID, *, request_id: str | None = None
    ) -> JobRow:
        """Queue a ``dead`` job again with a fresh attempt budget.

        An operator may retry the jobs of their own actions; any other job needs a tenant admin.

        Raises:
            ConflictError: If the job is not ``dead`` or ``failed``.
        """
        require(principal, "retry", "job_own")
        job = await self.get(principal, tenant_id, job_id)
        _require_job_role(principal, job, "retry")
        if not await queue.retry(self._db.admin, job.id):
            raise ConflictError(f"a {job.state} job cannot be retried; only dead jobs can")
        await self._audit(principal, tenant_id, job, "job.retried", request_id)
        return await self.get(principal, tenant_id, job_id)

    async def cancel(
        self, principal: Principal, tenant_id: UUID, job_id: UUID, *, request_id: str | None = None
    ) -> JobRow:
        """Cancel a job that has not started (same role rule as :meth:`retry`).

        A cancelled apply job puts its change set in ``failed``, so it can be applied again.

        Raises:
            ConflictError: If the job already started or finished.
        """
        require(principal, "cancel", "job_own")
        job = await self.get(principal, tenant_id, job_id)
        _require_job_role(principal, job, "cancel")
        if not await queue.cancel(self._db.admin, job.id):
            raise ConflictError(f"a {job.state} job cannot be cancelled; only queued jobs can")
        if job.kind == APPLY_KIND and job.change_set_id is not None:
            async with tenant_session(self._db.app, [tenant_id]) as session:
                record = await store.get_change_set(session, job.change_set_id, for_update=True)
                if record is not None and record.state is ChangeSetState.APPLYING:
                    await store.set_state(
                        session,
                        job.change_set_id,
                        ChangeSetState.FAILED,
                        error=f"apply cancelled by {principal.actor}",
                    )
        await self._audit(principal, tenant_id, job, "job.cancelled", request_id)
        return await self.get(principal, tenant_id, job_id)

    async def _audit(
        self,
        principal: Principal,
        tenant_id: UUID,
        job: JobRow,
        action: str,
        request_id: str | None,
    ) -> None:
        diff: dict[str, Any] = {"kind": job.kind, "state": job.state}
        async with admin_session(self._db.admin) as session:
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action=action,
                object_ref=f"job:{job.id}",
                diff=diff,
                request_id=request_id,
            )
