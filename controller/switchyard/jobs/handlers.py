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
from switchyard.services.alerts import EVALUATE_ALERTS_JOB, AlertEvaluator
from switchyard.services.certificates import (
    ISSUE_CERT_JOB,
    RENEW_CERTS_JOB,
    SYNC_CLOUDFLARE_IPS_JOB,
    CertificateService,
    issue_stages,
)
from switchyard.services.changesets import APPLY_JOB, REAPPLY_JOB
from switchyard.services.context import Databases
from switchyard.services.controller_settings import CONTROLLER_TICK_JOB, ControllerSettings
from switchyard.services.discovery import DISCOVER_POOLS_JOB, DiscoveryService
from switchyard.services.dns_records import SYNC_DNS_RECORDS_JOB, DnsRecordSync
from switchyard.services.gc import GC_JOB, GarbageCollector
from switchyard.services.gslb import PROBE_GSLB_JOB, SYNC_GSLB_JOB, GslbProber, GslbSync
from switchyard.services.infra import (
    APPLY_SDN_JOB,
    DEPLOY_SE_GROUP_JOB,
    RETIRE_ENGINE_JOB,
    DeployRequest,
    InfraService,
)
from switchyard.services.iplists import SYNC_IP_LISTS_JOB, IpListSync
from switchyard.services.jwks_sync import SYNC_JWKS_JOB, JwksSync
from switchyard.services.kubernetes import KUBERNETES_SYNC_JOB, KubernetesClusters
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
    controller: ControllerSettings | None = None,
    kubernetes: KubernetesClusters | None = None,
    members: MemberService | None = None,
    discovery: DiscoveryService | None = None,
    alerts: AlertEvaluator | None = None,
    iplists: IpListSync | None = None,
    gslb: GslbSync | None = None,
    prober: GslbProber | None = None,
    jwks: JwksSync | None = None,
    dns_records: DnsRecordSync | None = None,
) -> dict[str, Handler]:
    """Return the handler table for a worker.

    Args:
        db: Database handles.
        pipeline: The apply pipeline.
        members: Timed drain expiry; ``None`` skips the ``expire_drains`` kind.
        iplists: Country and reputation list sync; ``None`` skips the ``sync_ip_lists`` kind.
        dns_records: Publishes virtual services as DNS address records; ``None`` skips the
            ``sync_dns_records`` kind.
        jwks: Refreshes JWT keys from ``jwks_url``; ``None`` skips the ``sync_jwks`` kind.
        discovery: Pool member discovery; ``None`` skips the ``discover_pool_members`` kind.
        gc: Hourly housekeeping; ``None`` skips the ``gc`` kind.
        kubernetes: Syncs the tenants' Kubernetes clusters; ``None`` skips the ``kubernetes_sync``
            kind.
        controller: Checks controller settings changes that wait for confirmation; ``None`` skips
            the ``controller_tick`` kind.
        alerts: The alert evaluator; ``None`` skips the ``evaluate_alerts`` kind.
        gslb: The GSLB sync; ``None`` skips the ``sync_gslb`` kind.
        prober: The latency prober; ``None`` skips the ``probe_gslb`` kind.
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
    if kubernetes is not None:

        async def kubernetes_sync(job: Job) -> dict[str, Any]:
            return {"clusters": await kubernetes.sync_all()}

        handlers[KUBERNETES_SYNC_JOB] = kubernetes_sync
    if controller is not None:

        async def controller_tick(job: Job) -> dict[str, Any]:
            return {"events": await controller.tick()}

        handlers[CONTROLLER_TICK_JOB] = controller_tick
    if members is not None:

        async def expire_drains(job: Job) -> dict[str, Any]:
            return {"disabled": await members.expire_drains()}

        handlers[EXPIRE_DRAINS_JOB] = expire_drains
    if gc is not None:

        async def collect_garbage(job: Job) -> dict[str, Any]:
            return dict(await gc.run())

        handlers[GC_JOB] = collect_garbage
    if iplists is not None:

        async def sync_ip_lists(job: Job) -> dict[str, Any]:
            return {"changed": await iplists.run()}

        handlers[SYNC_IP_LISTS_JOB] = sync_ip_lists
    if jwks is not None:

        async def sync_jwks(job: Job) -> dict[str, Any]:
            return {"changed": await jwks.run()}

        handlers[SYNC_JWKS_JOB] = sync_jwks
    if discovery is not None:

        async def discover_pool_members(job: Job) -> dict[str, Any]:
            return {"changed": await discovery.run()}

        handlers[DISCOVER_POOLS_JOB] = discover_pool_members
    if alerts is not None:

        async def evaluate_alerts(job: Job) -> dict[str, Any]:
            return dict(await alerts.run())

        handlers[EVALUATE_ALERTS_JOB] = evaluate_alerts
    if gslb is not None:

        async def sync_gslb(job: Job) -> dict[str, Any]:
            return dict(await gslb.run())

        handlers[SYNC_GSLB_JOB] = sync_gslb
    if dns_records is not None:

        async def sync_dns_records(job: Job) -> dict[str, Any]:
            return dict(await dns_records.run())

        handlers[SYNC_DNS_RECORDS_JOB] = sync_dns_records
    if prober is not None:

        async def probe_gslb(job: Job) -> dict[str, Any]:
            return dict(await prober.run())

        handlers[PROBE_GSLB_JOB] = probe_gslb
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
