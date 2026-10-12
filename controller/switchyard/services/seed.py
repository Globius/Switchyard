# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Load the demo tenant into the database."""

from importlib import resources

import yaml
from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.db.repository import add_snapshot, find_tenant_id
from switchyard.db.tenancy import admin_session
from switchyard.domain.loader import reassign_ids, snapshot_from_mapping
from switchyard.domain.snapshot import Snapshot, ensure_valid
from switchyard.ids import uuid7
from switchyard.services.errors import AlreadyExistsError


def example_snapshot() -> Snapshot:
    """Return the demo tenant (``switchyard/examples/demo.yaml``) with fresh UUIDv7 ids.

    Example:
        >>> snap = example_snapshot()
        >>> snap.tenants[0].name
        'demo'
        >>> snap.tenants[0].id.version
        7
    """
    text = resources.files("switchyard.examples").joinpath("demo.yaml").read_text("utf-8")
    snapshot = reassign_ids(snapshot_from_mapping(yaml.safe_load(text)), uuid7)
    ensure_valid(snapshot)
    return snapshot


async def seed_example(admin: AsyncEngine) -> Snapshot:
    """Insert the demo tenant.

    Raises:
        AlreadyExistsError: If a tenant with the same name exists.
    """
    snapshot = example_snapshot()
    async with admin_session(admin) as session:
        name = snapshot.tenants[0].name
        if await find_tenant_id(session, name) is not None:
            raise AlreadyExistsError(f"tenant {name!r} already exists")
        await add_snapshot(session, snapshot)
    return snapshot
