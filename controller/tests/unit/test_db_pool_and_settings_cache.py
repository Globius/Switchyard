# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Found by the load test on the lab: connection churn and a stampede on the settings cache."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from switchyard.db import tenancy
from switchyard.db.tenancy import make_engine
from switchyard.services import controller_settings as cs


def test_the_pool_keeps_more_than_sqlalchemys_five_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SWITCHYARD_DB_POOL_SIZE", raising=False)
    monkeypatch.delenv("SWITCHYARD_DB_POOL_OVERFLOW", raising=False)
    pool: Any = make_engine("postgresql+psycopg://u:p@localhost/db").pool
    assert pool.size() == 8 and pool._max_overflow == 8 and pool._pre_ping is True


def test_the_pool_size_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWITCHYARD_DB_POOL_SIZE", "20")
    monkeypatch.setenv("SWITCHYARD_DB_POOL_OVERFLOW", "nonsense")
    pool: Any = make_engine("postgresql+psycopg://u:p@localhost/db").pool
    assert pool.size() == 20 and pool._max_overflow == 8  # a bad value falls back to the default


async def test_many_requests_at_cache_expiry_read_the_settings_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reads = 0

    @asynccontextmanager
    async def fake_session(_: object) -> Any:
        yield object()

    async def slow_row(self: Any, session: Any, *, lock: bool = False) -> Any:
        nonlocal reads
        reads += 1
        await asyncio.sleep(0.05)
        return SimpleNamespace(public_url="https://x.test", pending=None)

    monkeypatch.setattr(cs, "admin_session", fake_session)
    monkeypatch.setattr(cs.ControllerSettings, "_row", slow_row)
    settings = cs.ControllerSettings(SimpleNamespace(admin=object()), object())  # type: ignore[arg-type]
    answers = await asyncio.gather(*(settings.public_url("x.test", None) for _ in range(50)))
    assert set(answers) == {"https://x.test"} and reads == 1
    assert tenancy.make_engine  # the module under test is imported
