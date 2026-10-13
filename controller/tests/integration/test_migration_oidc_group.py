# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Migration 0012 copies each ``tenant.oidc_group`` into the audit log, then drops the column."""

import json
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from testcontainers.community.postgres import PostgresContainer

from switchyard.db import migrate


@pytest.fixture
def fresh_pg() -> Iterator[str]:
    """A PostgreSQL of its own, so migrating up and down cannot disturb the shared one."""
    with PostgresContainer("postgres:17", driver="psycopg") as pg:
        yield pg.get_connection_url()


def test_old_oidc_group_values_land_in_the_audit_log(fresh_pg: str) -> None:
    migrate.upgrade(fresh_pg, "0011")
    engine = create_engine(fresh_pg)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenant (id, name, oidc_group) VALUES "
                "(gen_random_uuid(), 'acme', 'acme-admins'), "
                "(gen_random_uuid(), 'plain', NULL), (gen_random_uuid(), 'empty', '')"
            )
        )
    migrate.upgrade(fresh_pg)
    with engine.begin() as conn:
        columns = conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name='tenant'")
        ).scalars()
        assert "oidc_group" not in set(columns)
        events = conn.execute(
            text(
                "SELECT actor, object_ref, diff FROM audit_event "
                "WHERE action = 'tenant.oidc_group_dropped'"
            )
        ).all()
    assert len(events) == 1, "only non-empty values are logged"
    actor, object_ref, diff = events[0]
    assert (actor, object_ref) == ("system:migration", "tenant:acme")
    assert (diff if isinstance(diff, dict) else json.loads(diff)) == {"oidc_group": "acme-admins"}
    engine.dispose()
