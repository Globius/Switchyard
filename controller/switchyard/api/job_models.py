"""Response models for the job endpoints (ticket T5, ADR 0016)."""

__all__ = ["Job", "JobKind", "JobLog", "JobStage", "LogLine", "Page"]

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from switchyard.api.schemas import ApiModel, Page
from switchyard.db.ops_models import JobRow
from switchyard.domain.workflow import JobState
from switchyard.jobs.progress import StageState


class JobKind(StrEnum):
    """Every kind of background job, for filters and labels.

    Attributes:
        APPLY_CHANGE_SET: Applies a change set (stages lock, render, validate, canary, rollout,
            commit). Result: `ApplyJobResult`.
        REAPPLY_SE_GROUP: Pushes the current desired state to one SE group again to repair drift
            (stages lock, render, validate, canary, rollout; no revision). Result:
            `{ok, se_group_id, se_group, revision_number, error, details}`.
        DEPLOY_SE_GROUP: Clones SE VMs on Proxmox for an SE group.
        RETIRE_ENGINE: Destroys an engine's VM and removes its record.
        APPLY_SDN: Creates or updates the Proxmox SDN objects of managed networks.
        ISSUE_CERT: Issues or renews one certificate; result `{change_set_id}` of the
            `system:certs` change set that records it.
        RENEW_CERTS: Scheduled sweep that renews certificates due for renewal.
        SYNC_CLOUDFLARE_IPS: Scheduled refresh of Cloudflare-managed allow lists.
        POLL_STATUS: Scheduled engine status poll and drift detection.
        GIT_MIRROR: Pushes a revision to the Git mirror.
        GC: Scheduled housekeeping.
        PROBE_GSLB: Measures round-trip times to the members of latency wide IPs.
        SYNC_GSLB: Follows virtual service VIP moves in GSLB records.
        KUBERNETES_SYNC: Scheduled sync of every enabled Kubernetes cluster: reads Services and
            Gateways and applies the difference as a `system:k8s` change set.
        CONTROLLER_TICK: Scheduled check of a controller settings change that waits for
            confirmation; reverts it when the window runs out.
        DISCOVER_POOL_MEMBERS: Pool member discovery from Proxmox tags.
        SYNC_IP_LISTS: Refreshes country and reputation lists from their sources.
        EVALUATE_ALERTS: Checks every tenant's alert rules and sends notifications.
    """

    APPLY_CHANGE_SET = "apply_change_set"
    REAPPLY_SE_GROUP = "reapply_se_group"
    DEPLOY_SE_GROUP = "deploy_se_group"
    RETIRE_ENGINE = "retire_engine"
    APPLY_SDN = "apply_sdn"
    ISSUE_CERT = "issue_cert"
    RENEW_CERTS = "renew_certs"
    SYNC_CLOUDFLARE_IPS = "sync_cloudflare_ips"
    POLL_STATUS = "poll_status"
    GIT_MIRROR = "git_mirror"
    GC = "gc"
    CONTROLLER_TICK = "controller_tick"
    KUBERNETES_SYNC = "kubernetes_sync"
    DISCOVER_POOL_MEMBERS = "discover_pool_members"
    SYNC_IP_LISTS = "sync_ip_lists"
    EVALUATE_ALERTS = "evaluate_alerts"
    SYNC_GSLB = "sync_gslb"
    PROBE_GSLB = "probe_gslb"


class JobStage(ApiModel):
    """One step of a job, with timing.

    The apply pipeline has lock, render, validate, canary, rollout and commit.
    """

    name: str
    state: StageState
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class GroupResult(ApiModel):
    """What an apply did to one SE group."""

    se_group_id: UUID
    se_group: str
    state: Literal["pending", "succeeded", "failed", "reverted"] | str
    rendered_hash: str | None = None
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ApplyJobResult(ApiModel):
    """Result of an ``apply_change_set`` job.

    ``ok: false`` means the change set was rejected or reverted: the job *finished* and will not be
    retried. Compare ``Job.retrying``.
    """

    ok: bool
    change_set_id: UUID
    revision_number: int | None = None
    error: str | None = None
    groups: list[GroupResult] = Field(default_factory=list)


class Job(ApiModel):
    """A background job."""

    id: UUID
    kind: JobKind
    state: JobState
    tenant_id: UUID | None = None
    change_set_id: UUID | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempts: int
    max_attempts: int
    error: str | None = Field(None, description="Error of the latest failed attempt.")
    retrying: bool = Field(
        description="True while a failed attempt waits for its next try (state `queued`, "
        "attempts > 0). A reverted or rejected apply is not retrying: it is `succeeded` with "
        "`ok: false`."
    )
    ok: bool | None = Field(
        None, description="For finished jobs that report one (apply): whether the work succeeded."
    )
    result: ApplyJobResult | dict[str, Any] | None = None
    stages: list[JobStage] = Field(default_factory=list)

    @classmethod
    def from_row(cls, row: JobRow) -> "Job":
        """Build the API form of a job row."""
        result = row.result
        ok = result.get("ok") if isinstance(result, dict) and "ok" in result else None
        return cls(
            id=row.id,
            kind=JobKind(row.kind),
            state=JobState(row.state),
            tenant_id=row.tenant_id,
            change_set_id=row.change_set_id,
            created_at=row.created_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            error=row.last_error,
            retrying=row.state == JobState.QUEUED.value and row.attempts > 0,
            ok=ok if isinstance(ok, bool) else None,
            result=result,
            stages=[JobStage.model_validate(s) for s in row.stages],
        )


class LogLine(ApiModel):
    """One line of a job's log."""

    at: datetime
    level: Literal["info", "error"]
    message: str


class JobLog(ApiModel):
    """The log of a job (the latest 500 lines)."""

    job_id: UUID
    lines: list[LogLine]
