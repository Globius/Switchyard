"""Job handlers: ``apply_change_set`` and ``git_mirror``."""

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select, update

from switchyard.adapters.git import GitMirror, revision_files
from switchyard.db import audit_log
from switchyard.db import changesets as store
from switchyard.db.models import TenantRow
from switchyard.db.ops_models import RevisionRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.snapshot import Snapshot
from switchyard.jobs.queue import Job
from switchyard.jobs.worker import Handler
from switchyard.pipeline.apply import ApplyPipeline
from switchyard.services.changesets import APPLY_JOB
from switchyard.services.context import Databases
from switchyard.services.status import StatusService

log = logging.getLogger("switchyard.jobs")
GIT_MIRROR_JOB = "git_mirror"


POLL_STATUS_JOB = "poll_status"


def build_handlers(
    db: Databases,
    pipeline: ApplyPipeline,
    mirror: GitMirror | None,
    status: StatusService | None = None,
) -> dict[str, Handler]:
    """Return the handler table for a worker.

    Args:
        db: Database handles.
        pipeline: The apply pipeline.
        status: Status poller; when ``None`` the ``poll_status`` kind is not handled.
        mirror: Git mirror; when ``None`` the ``git_mirror`` kind is not handled (jobs wait in the
            queue until a worker with a mirror runs).
    """

    async def apply_change_set(job: Job) -> dict[str, Any]:
        tenant_id = UUID(job.payload["tenant_id"])
        change_set_id = UUID(job.payload["change_set_id"])
        report = await pipeline.run(tenant_id, change_set_id, request_id=job.request_id)
        return report.as_dict()

    handlers: dict[str, Handler] = {APPLY_JOB: apply_change_set}
    if status is not None:

        async def poll_status(job: Job) -> dict[str, Any]:
            return {"polled": await status.poll_all()}

        handlers[POLL_STATUS_JOB] = poll_status
    if mirror is not None:

        async def git_mirror(job: Job) -> dict[str, Any]:
            tenant_id = UUID(job.payload["tenant_id"])
            number = int(job.payload["revision_number"])
            async with tenant_session(db.app, [tenant_id]) as session:
                revision = await store.get_revision(session, tenant_id, number)
                tenant_name = (
                    await session.execute(select(TenantRow.name).where(TenantRow.id == tenant_id))
                ).scalar_one()
            if revision is None:
                return {"skipped": f"revision {number} no longer exists"}
            snapshot = Snapshot.model_validate(revision.snapshot)
            sha = await mirror.commit(
                f"tenants/{tenant_name}/",
                revision_files(tenant_name, snapshot),
                f"{tenant_name} revision {number}: {revision.summary or 'no summary'}\n\n"
                f"Author: {revision.author}",
            )
            async with admin_session(db.admin) as session:
                await session.execute(
                    update(RevisionRow)
                    .where(RevisionRow.id == revision.id)
                    .values(git_sha=sha or None)
                )
                await audit_log.record(
                    session,
                    tenant_id=tenant_id,
                    actor="system:git_mirror",
                    action="revision.mirrored",
                    object_ref=f"revision:{number}",
                    diff={"sha": sha},
                    request_id=job.request_id,
                )
            return {"sha": sha}

        handlers[GIT_MIRROR_JOB] = git_mirror
    return handlers
