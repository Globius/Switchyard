"""Shared pytest fixtures.

Tests under ``tests/integration`` need Docker (PostgreSQL via testcontainers) and are marked
``docker`` automatically. Run ``pytest -m "not docker"`` for the fast subset.
"""

import asyncio
import sys
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from switchyard.db import migrate
from switchyard.db.tenancy import ADMIN_ROLE, APP_ROLE, make_engine
from tests.lab import Lab

if sys.platform == "win32":  # psycopg cannot use the default ProactorEventLoop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything under ``tests/integration`` with ``docker``."""
    for item in items:
        if "tests/integration" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.docker)


@pytest.fixture(scope="session")
def lab_network() -> Iterator[Lab]:
    """The Docker lab network (RFC 5737 range), shared by every test that needs real engines.

    Tests start their containers inside ``lab_network.scope()`` so they are removed per module.
    """
    instance = Lab()
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """URL of a migrated PostgreSQL 17 container, shared by the session."""
    with PostgresContainer("postgres:17", driver="psycopg") as pg:
        url = pg.get_connection_url()
        migrate.upgrade(url)
        yield url


@pytest_asyncio.fixture
async def owner_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    """Engine connected as the superuser that owns the schema. Tables are emptied afterwards."""
    engine = make_engine(pg_url)
    yield engine
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE tenant, identity_provider, user_session, local_admin, break_glass_setting CASCADE"
            )
        )
    await engine.dispose()


@pytest_asyncio.fixture
async def admin_engine(pg_url: str, owner_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Engine connected as the RLS-bypassing admin role."""
    engine = make_engine(pg_url, role=ADMIN_ROLE)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def app_engine(pg_url: str, owner_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """Engine connected as the RLS-subject application role."""
    engine = make_engine(pg_url, role=APP_ROLE)
    yield engine
    await engine.dispose()
