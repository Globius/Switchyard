# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""One apply at a time per SE group, using PostgreSQL advisory locks.

Two change sets that touch different SE groups run in parallel; two that touch the same group
queue behind each other (backend design, "Change pipeline"). The lock is a *session-level* advisory
lock on a dedicated connection, so it survives the many short transactions an apply performs and is
released by PostgreSQL itself if the worker dies.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class LockTimeoutError(TimeoutError):
    """Another apply held an SE group's lock for too long."""


def lock_key(se_group_id: UUID) -> int:
    """Map an SE group id to a signed 64-bit advisory-lock key.

    Uses the random low bytes of the (UUIDv7) id, so keys of different groups differ.

    Example:
        >>> from uuid import UUID
        >>> lock_key(UUID(int=1))
        1
        >>> lock_key(UUID(int=2**64 - 1)) < 0
        True
    """
    return int.from_bytes(se_group_id.bytes[8:], "big", signed=True)


class PgGroupLocks:
    """Advisory locks held on connections of ``engine``."""

    def __init__(
        self, engine: AsyncEngine, *, timeout_s: float = 300.0, poll_s: float = 0.2
    ) -> None:
        """Create the lock manager.

        Args:
            engine: Any engine for the same database (the admin engine is used in practice).
            timeout_s: How long to wait for a busy lock before giving up.
            poll_s: Delay between attempts.
        """
        self._engine = engine
        self._timeout = timeout_s
        self._poll = poll_s

    @asynccontextmanager
    async def hold(self, se_group_ids: Iterable[UUID]) -> AsyncIterator[None]:
        """Hold the locks of every group in ``se_group_ids`` (acquired in sorted order)."""
        keys = sorted({lock_key(i) for i in se_group_ids})
        connection = await self._engine.connect()
        acquired: list[int] = []
        try:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            for key in keys:
                await self._acquire(connection, key)
                acquired.append(key)
            yield
        finally:
            for key in reversed(acquired):
                await connection.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            await connection.close()

    async def _acquire(self, connection: AsyncConnection, key: int) -> None:
        deadline = time.monotonic() + self._timeout
        while True:
            got = (
                await connection.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key})
            ).scalar_one()
            if got:
                return
            if time.monotonic() > deadline:
                raise LockTimeoutError(f"advisory lock {key} is busy")
            await asyncio.sleep(self._poll)
