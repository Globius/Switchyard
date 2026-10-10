"""The worker loop: claim a job, run its handler with heartbeats, record the outcome."""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.jobs import queue
from switchyard.jobs.queue import Job

log = logging.getLogger("switchyard.worker")

#: A handler receives the claimed job and returns a JSON-serialisable result (or ``None``).
Handler = Callable[[Job], Awaitable[dict[str, Any] | None]]


class Worker:
    """Runs jobs from the PostgreSQL queue.

    A handler that raises is retried with exponential backoff until ``max_attempts``. A handler that
    returns normally ends the job, even when its result reports a failure of the business operation
    (for example an apply that was reverted): repeating a canary failure would not help.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        handlers: Mapping[str, Handler],
        *,
        worker_id: str,
        poll_interval_s: float = 1.0,
        heartbeat_interval_s: float = 10.0,
        stale_after: timedelta = timedelta(seconds=60),
    ) -> None:
        """Create a worker.

        Args:
            engine: Admin engine (the queue is cross-tenant).
            handlers: ``kind -> handler``.
            worker_id: Unique label recorded on claimed jobs.
            poll_interval_s: Sleep between polls when the queue is empty.
            heartbeat_interval_s: How often a running job reports it is alive.
            stale_after: Silence after which another worker may reclaim a job.
        """
        self._engine = engine
        self._handlers = dict(handlers)
        self._worker_id = worker_id
        self._poll = poll_interval_s
        self._beat = heartbeat_interval_s
        self._stale_after = stale_after

    async def run_once(self) -> bool:
        """Claim and run at most one job. Returns True if a job was processed."""
        job = await queue.claim(self._engine, self._worker_id, set(self._handlers))
        if job is None:
            return False
        log.info("job %s (%s) attempt %d claimed", job.id, job.kind, job.attempts)
        beat = asyncio.create_task(self._heartbeat(job))
        try:
            result = await self._handlers[job.kind](job)
        except Exception as exc:
            log.exception("job %s (%s) failed", job.id, job.kind)
            state = await queue.fail(self._engine, job.id, f"{type(exc).__name__}: {exc}")
            log.warning("job %s is now %s", job.id, state)
        else:
            await queue.succeed(self._engine, job.id, result)
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        return True

    async def _heartbeat(self, job: Job) -> None:
        while True:
            await asyncio.sleep(self._beat)
            await queue.heartbeat(self._engine, job.id)

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Process jobs until ``stop`` is set."""
        while not stop.is_set():
            await queue.reclaim_stale(self._engine, self._stale_after)
            if not await self.run_once():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=self._poll)
