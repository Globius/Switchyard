"""Per-job stage and log recording.

A long job (the apply pipeline) reports named stages so the UI and ``syctl`` can show where it is
and which stage failed. Stages and log lines are stored on the ``job`` row (JSON) and written with
the admin engine, because the application role may not update jobs.

A stage entry is ``{name, state, started_at, finished_at, error}`` with ``state`` one of
:class:`StageState`. A stage that runs more than once (one rollout per SE group) keeps one entry:
``started_at`` is the first start and ``finished_at`` the last finish.
"""

import enum
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.db.ops_models import JobRow
from switchyard.db.tenancy import admin_session

#: Log lines kept per job; older ones are dropped.
MAX_LOG_LINES = 500
#: Apply stages in order (backend design, "Change pipeline").
APPLY_STAGES = ("lock", "render", "validate", "canary", "rollout", "commit")


class StageState(enum.StrEnum):
    """State of one job stage."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def log_line(level: str, message: str) -> dict[str, Any]:
    """Return one log entry ``{at, level, message}``."""
    return {"at": _now(), "level": level, "message": message[:2000]}


class JobProgress:
    """Records stages and log lines for one job.

    Example:
        >>> JobProgress(None, None).enabled
        False
    """

    def __init__(self, engine: AsyncEngine | None, job_id: UUID | None) -> None:
        """Create a recorder; with no job it does nothing (the pipeline also runs outside a job)."""
        self._engine = engine
        self._job_id = job_id

    @property
    def enabled(self) -> bool:
        """True when stages are being stored."""
        return self._engine is not None and self._job_id is not None

    async def _update(self, change: Any) -> None:
        if self._engine is None or self._job_id is None:
            return
        async with admin_session(self._engine) as session:
            row = (
                await session.execute(
                    select(JobRow).where(JobRow.id == self._job_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return
            stages = [dict(s) for s in row.stages]
            log = list(row.log)
            change(stages, log)
            row.stages = stages
            row.log = log[-MAX_LOG_LINES:]

    async def plan(self, names: Sequence[str]) -> None:
        """Announce the stages this run will go through, all ``pending``, replacing older ones."""

        def change(stages: list[dict[str, Any]], log: list[dict[str, Any]]) -> None:
            stages[:] = [_stage(n) for n in names]

        await self._update(change)

    async def begin(self, name: str) -> None:
        """Mark a stage running."""

        def change(stages: list[dict[str, Any]], log: list[dict[str, Any]]) -> None:
            entry = _entry(stages, name)
            entry["started_at"] = entry["started_at"] or _now()
            entry.update(state=StageState.RUNNING.value, finished_at=None, error=None)
            log.append(log_line("info", f"{name}: started"))

        await self._update(change)

    async def end(self, name: str, error: str | None = None) -> None:
        """Mark a stage finished: ``succeeded``, or ``failed`` with ``error``."""

        def change(stages: list[dict[str, Any]], log: list[dict[str, Any]]) -> None:
            entry = _entry(stages, name)
            entry["finished_at"] = _now()
            if error is None:
                entry["state"] = StageState.SUCCEEDED.value
                log.append(log_line("info", f"{name}: done"))
            else:
                entry.update(state=StageState.FAILED.value, error=error[:2000])
                log.append(log_line("error", f"{name}: {error}"))

        await self._update(change)

    @asynccontextmanager
    async def stage(self, name: str) -> AsyncIterator[None]:
        """Run a block as a stage; an exception marks it failed and propagates."""
        await self.begin(name)
        try:
            yield
        except BaseException as exc:
            await self.end(name, str(exc) or type(exc).__name__)
            raise
        await self.end(name)

    async def note(self, message: str, level: str = "info") -> None:
        """Append a log line."""

        def change(stages: list[dict[str, Any]], log: list[dict[str, Any]]) -> None:
            log.append(log_line(level, message))

        await self._update(change)

    async def skip_pending(self) -> None:
        """Mark stages that never ran ``skipped`` (the run ended early)."""

        def change(stages: list[dict[str, Any]], log: list[dict[str, Any]]) -> None:
            for entry in stages:
                if entry["state"] == StageState.PENDING.value:
                    entry["state"] = StageState.SKIPPED.value

        await self._update(change)


def _stage(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "state": StageState.PENDING.value,
        "started_at": None,
        "finished_at": None,
        "error": None,
    }


def _entry(stages: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for entry in stages:
        if entry["name"] == name:
            return entry
    entry = _stage(name)
    stages.append(entry)
    return entry
