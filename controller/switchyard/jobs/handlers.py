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
from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot
from switchyard.jobs.progress import JobProgress
from switchyard.jobs.queue import Job
from switchyard.jobs.worker import Handler
from switchyard.pipeline.apply import ApplyPipeline
from switchyard.services.certificates import (
    ISSUE_CERT_JOB,
    RENEW_CERTS_JOB,
    SYNC_CLOUDFLARE_IPS_JOB,
    CertificateService,
    issue_stages,
)
from switchyard.services.changesets import APPLY_JOB, REAPPLY_JOB
from switchyard.services.context import Databases
from switchyard.services.gc import GC_JOB, GarbageCollector
from switchyard.services.infra import (
    APPLY_SDN_JOB,
    DEPLOY_SE_GROUP_JOB,
    RETIRE_ENGINE_JOB,
    DeployRequest,
    InfraService,
)
from switchyard.services.members import MemberService
from switchyard.services.status import StatusService

log = logging.getLogger("switchyard.jobs")
GIT_MIRROR_JOB = "git_mirror"


POLL_STATUS_JOB = "poll_status"
EXPIRE_DRAINS_JOB = "expire_drains"


def build_handlers(
    db: Databases,
    pipeline: ApplyPipeline,
    mirror: GitMirror | None,
    status: StatusService | None = None,
    infra: InfraService | None = None,
    certs: CertificateService | None = None,
    gc: GarbageCollector | None = None,
    members: MemberService | None = None,
) -> dict[str, Handler]:
    """Return the handler table for a worker.

    Args:
        db: Database handles.
        pipeline: The apply pipeline.
        members: Timed drain expiry; ``None`` skips the ``expire_drains`` kind.
        gc: Hourly housekeeping; ``None`` skips the ``gc`` kind.
        certs: Certificate issuance, renewal and the Cloudflare IP sync; ``None`` skips those kinds.
        infra: Proxmox-backed SDN and deploy jobs; when ``None`` those kinds are not handled.
        status: Status poller; when ``None`` the ``poll_status`` kind is not handled.
        mirror: Git mirror; when ``None`` the ``git_mirror`` kind is not handled (jobs wait in the
            queue until a worker with a mirror runs).
    """

    async def apply_change_set(job: Job) -> dict[str, Any]:
        tenant_id = UUID(job.payload["tenant_id"])
        change_set_id = UUID(job.payload["change_set_id"])
        report = await pipeline.run(
            tenant_id,
            change_set_id,
            request_id=job.request_id,
            progress=JobProgress(db.admin, job.id),
        )
        return report.as_dict()

    async def reapply_se_group(job: Job) -> dict[str, Any]:
        report = await pipeline.reapply(
            UUID(job.payload["tenant_id"]),
            UUID(job.payload["se_group_id"]),
            request_id=job.request_id,
            progress=JobProgress(db.admin, job.id),
        )
        return report.as_dict()

    handlers: dict[str, Handler] = {APPLY_JOB: apply_change_set, REAPPLY_JOB: reapply_se_group}
    if members is not None:

        async def expire_drains(job: Job) -> dict[str, Any]:
            return {"disabled": await members.expire_drains()}

        handlers[EXPIRE_DRAINS_JOB] = expire_drains
    if gc is not None:

        async def collect_garbage(job: Job) -> dict[str, Any]:
            return dict(await gc.run())

        handlers[GC_JOB] = collect_garbage
    if certs is not None:

        async def issue_cert(job: Job) -> dict[str, Any]:
            p = job.payload
            source = dm.CertSource(p["source"])
            progress = JobProgress(db.admin, job.id)
            await progress.plan(issue_stages(source))
            change_set = await certs.issue(
                UUID(p["tenant_id"]),
                p["name"],
                source,
                tuple(p["sans"]),
                renew=dm.RenewPolicy(p["renew_policy"]) if p.get("renew_policy") else None,
                renew_before_days=p.get("renew_before_days"),
                acme_provider=p.get("acme_provider"),
                challenge=dm.AcmeChallenge(p["challenge"]) if p.get("challenge") else None,
                dns_provider=p.get("dns_provider"),
                progress=progress,
            )
            await progress.skip_pending()
            return {"change_set_id": str(change_set)}

        async def renew_certs(job: Job) -> dict[str, Any]:
            return {"renewed": await certs.renew_due()}

        async def sync_cloudflare_ips(job: Job) -> dict[str, Any]:
            return {"changed": await certs.sync_cloudflare_ips()}

        handlers[ISSUE_CERT_JOB] = issue_cert
        handlers[RENEW_CERTS_JOB] = renew_certs
        handlers[SYNC_CLOUDFLARE_IPS_JOB] = sync_cloudflare_ips
    if infra is not None:

        async def apply_sdn(job: Job) -> dict[str, Any]:
            return await infra.apply_sdn(UUID(job.payload["tenant_id"]))

        async def deploy_se_group(job: Job) -> dict[str, Any]:
            return await infra.deploy_se_group(DeployRequest.from_payload(job.payload))

        async def retire_engine(job: Job) -> dict[str, Any]:
            return await infra.retire_engine(
                UUID(job.payload["tenant_id"]), UUID(job.payload["engine_id"])
            )

        handlers[APPLY_SDN_JOB] = apply_sdn
        handlers[RETIRE_ENGINE_JOB] = retire_engine
        handlers[DEPLOY_SE_GROUP_JOB] = deploy_se_group
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
                + (f"{revision.message}\n\n" if revision.message else "")
                + f"Author: {revision.author}",
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
