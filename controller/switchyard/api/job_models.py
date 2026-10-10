"""Response models for the job endpoints (ticket T5, ADR 0016)."""

__all__ = ["Job", "JobLog", "JobStage", "LogLine", "Page"]

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from switchyard.api.schemas import ApiModel, Page
from switchyard.db.ops_models import JobRow
from switchyard.domain.workflow import JobState
from switchyard.jobs.progress import StageState


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
    kind: str
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
            kind=row.kind,
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
