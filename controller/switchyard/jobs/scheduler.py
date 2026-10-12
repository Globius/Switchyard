# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Periodic jobs, scheduled by whichever worker holds a PostgreSQL advisory lock.

No separate cron service is needed (backend design, "Background jobs"). Every worker may run a
:class:`Scheduler`; only the one that holds the lock enqueues. A kind is enqueued again only when no
job of that kind is waiting or running and the last one was created at least ``every`` ago, so a
slow poll never piles up behind itself and a new leader after a crash does not double-schedule.
"""

import asyncio
import contextlib
from datetime import timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from switchyard.db.ops_models import JobRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import JobState
from switchyard.jobs import queue

#: Advisory-lock key for scheduler leadership ("SYSCHED" in hex-ish ASCII).
LEADER_KEY = 0x535953434845


class Scheduler:
    """Enqueues recurring jobs while it is the leader."""

    def __init__(self, engine: AsyncEngine, schedule: dict[str, timedelta]) -> None:
        """Create a scheduler.

        Args:
            engine: Admin engine.
            schedule: ``job kind -> interval``.
        """
        self._engine = engine
        self._schedule = schedule
        self._leader_conn: AsyncConnection | None = None

    async def try_lead(self) -> bool:
        """Try to become (or stay) the leader. The lock lives as long as the connection."""
        if self._leader_conn is None:
            connection = await self._engine.connect()
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            got = (
                await connection.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LEADER_KEY})
            ).scalar_one()
            if not got:
                await connection.close()
                return False
            self._leader_conn = connection
        return True

    async def resign(self) -> None:
        """Give up leadership (the lock is released by closing the connection)."""
        if self._leader_conn is not None:
            connection, self._leader_conn = self._leader_conn, None
            try:
                # Closing only returns a pooled connection, which would keep the session lock.
                await connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LEADER_KEY})
            finally:
                await connection.close()

    async def tick(self) -> list[str]:
        """Enqueue whatever is due. Returns the kinds enqueued (empty for non-leaders)."""
        if not await self.try_lead():
            return []
        due: list[str] = []
        async with admin_session(self._engine) as session:
            for kind, every in sorted(self._schedule.items()):
                busy = (
                    await session.execute(
                        select(func.count()).where(
                            JobRow.kind == kind,
                            JobRow.state.in_([JobState.QUEUED.value, JobState.RUNNING.value]),
                        )
                    )
                ).scalar_one()
                recent = (
                    await session.execute(
                        select(func.count()).where(
                            JobRow.kind == kind, JobRow.created_at > func.now() - every
                        )
                    )
                ).scalar_one()
                if not busy and not recent:
                    await queue.enqueue(session, kind, {}, max_attempts=1)
                    due.append(kind)
        return due

    async def run_forever(self, stop: asyncio.Event, poll_s: float = 1.0) -> None:
        """Tick until ``stop`` is set."""
        try:
            while not stop.is_set():
                await self.tick()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=poll_s)
        finally:
            await self.resign()
