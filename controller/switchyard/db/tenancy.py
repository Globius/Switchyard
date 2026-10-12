# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tenant scoping for database sessions.

Every API request (and every service call made on behalf of a tenant) runs in a transaction whose
``app.tenant_ids`` setting lists the tenants it may see. The RLS policies created by the migration
compare ``tenant_id`` against that setting, so the database refuses cross-tenant access even if a
service-layer check is missing.

System administration uses a separate database role (``switchyard_admin``, ``BYPASSRLS``). Code
never turns RLS off; it connects as a different role.
"""

import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

#: Role used by the API and tenant-scoped workers. Subject to RLS.
APP_ROLE = "switchyard_app"
#: Role used by system administration, migrations' seed data and cross-tenant jobs. Bypasses RLS.
ADMIN_ROLE = "switchyard_admin"
#: The session setting that the RLS policies read.
TENANT_SETTING = "app.tenant_ids"

_ROLE = re.compile(r"[a-z_][a-z0-9_]*")


def make_engine(url: str, *, role: str | None = None) -> AsyncEngine:
    """Create an async engine, optionally pinning every connection to a database role.

    Args:
        url: SQLAlchemy URL using the ``postgresql+psycopg`` driver.
        role: Role to assume on connect (``APP_ROLE`` or ``ADMIN_ROLE``). The connecting user must
            be a member of it. Production deployments connect as a dedicated login role instead.

    Returns:
        The engine. Dispose it when done.

    Example:
        >>> engine = make_engine("postgresql+psycopg://u:p@localhost/db", role=APP_ROLE)
        >>> engine.dialect.name
        'postgresql'
    """
    connect_args: dict[str, str] = {}
    if role is not None:
        if not _ROLE.fullmatch(role):
            raise ValueError(f"invalid role name {role!r}")
        connect_args["options"] = f"-c role={role}"
    return create_async_engine(url, connect_args=connect_args)


def format_tenant_ids(tenant_ids: Iterable[UUID]) -> str:
    """Serialise tenant ids for ``app.tenant_ids``: sorted, comma-separated, empty for none.

    Example:
        >>> from uuid import UUID
        >>> format_tenant_ids([UUID(int=2), UUID(int=1)])
        '00000000-0000-0000-0000-000000000001,00000000-0000-0000-0000-000000000002'
        >>> format_tenant_ids([])
        ''
    """
    return ",".join(sorted({str(t) for t in tenant_ids}))


async def set_tenant_scope(session: AsyncSession, tenant_ids: Iterable[UUID]) -> None:
    """Limit the current transaction to ``tenant_ids`` (transaction-local, like ``SET LOCAL``)."""
    await session.execute(
        text("SELECT set_config(:name, :value, true)"),
        {"name": TENANT_SETTING, "value": format_tenant_ids(tenant_ids)},
    )


@asynccontextmanager
async def tenant_session(
    engine: AsyncEngine, tenant_ids: Iterable[UUID]
) -> AsyncIterator[AsyncSession]:
    """Open a transaction scoped to ``tenant_ids``; commit on success, roll back on error.

    The scope is fixed before the caller runs any statement. With an empty ``tenant_ids`` the
    session sees nothing.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        await set_tenant_scope(session, tenant_ids)
        yield session


@asynccontextmanager
async def admin_session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Open a transaction without tenant scoping. The engine must connect as the admin role."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session, session.begin():
        yield session
