"""Row-level security and schema tests against a real PostgreSQL 17."""

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.db import models as m
from switchyard.db.repository import add_snapshot, load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from tests.factories import base_data

SYMBOLS = ["t-acme", "net-lb", "net-be", "vp-main", "seg-1", "cert-wild", "pool-web", "vs-web"]


def tenant_data(suffix: str, tenant_name: str) -> dict[str, Any]:
    """A copy of the base data whose symbolic ids carry ``suffix``, owned by ``tenant_name``."""
    raw = json.dumps(base_data())
    for old in SYMBOLS:
        raw = raw.replace(f'"{old}"', f'"{old}-{suffix}"')
    data: dict[str, Any] = json.loads(raw)
    data["tenants"][0]["name"] = tenant_name
    return data


def tid(symbol: str) -> uuid.UUID:
    return uuid.UUID(symbolic_uuid(symbol))


A, B = tid("t-acme-a"), tid("t-acme-b")

SCOPED_TABLES = [
    "network",
    "vip_pool",
    "se_group",
    "se_group_network",
    "health_monitor",
    "certificate",
    "policy_set",
    "pool",
    "pool_member",
    "virtual_service",
    "vs_port",
    "vs_route",
    "vs_certificate",
    "vs_policy_set",
]


async def seed_both(admin_engine: AsyncEngine) -> None:
    async with admin_session(admin_engine) as s:
        await add_snapshot(s, snapshot_from_mapping(tenant_data("a", "acme")))
        await add_snapshot(s, snapshot_from_mapping(tenant_data("b", "globex")))


async def test_every_tenant_scoped_table_has_forced_rls_and_a_policy(
    admin_engine: AsyncEngine,
) -> None:
    async with admin_session(admin_engine) as s:
        rls = {
            r[0]: r[1:]
            for r in (
                await s.execute(
                    text(
                        "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
                        " (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid)"
                        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                        " WHERE n.nspname = 'public' AND c.relkind = 'r'"
                    )
                )
            ).all()
        }
        with_tenant_id = {
            r[0]
            for r in (
                await s.execute(
                    text(
                        "SELECT table_name FROM information_schema.columns"
                        " WHERE table_schema = 'public' AND column_name = 'tenant_id'"
                    )
                )
            ).all()
        }
    assert with_tenant_id == set(m.TENANT_SCOPED_TABLES)
    assert set(SCOPED_TABLES) <= with_tenant_id
    for name in [*with_tenant_id, "tenant"]:
        enabled, forced, policies = rls[name]
        assert enabled, f"{name}: RLS not enabled"
        assert forced, f"{name}: RLS not forced"
        assert policies >= 1, f"{name}: no policy"


async def test_app_role_is_subject_to_rls_and_admin_role_bypasses(
    admin_engine: AsyncEngine,
) -> None:
    async with admin_session(admin_engine) as s:
        rows = (
            await s.execute(
                text(
                    "SELECT rolname, rolbypassrls, rolsuper FROM pg_roles"
                    " WHERE rolname LIKE 'switchyard\\_%'"
                )
            )
        ).all()
    assert {r[0]: (r[1], r[2]) for r in rows} == {
        "switchyard_app": (False, False),
        "switchyard_admin": (True, False),
    }


async def test_tenant_cannot_read_other_tenants_rows(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    await seed_both(admin_engine)
    async with tenant_session(app_engine, [A]) as s:
        snap = await load_snapshot(s)
        assert [t.name for t in snap.tenants] == ["acme"]
        assert {n.tenant_id for n in snap.networks} == {A}
        for table in SCOPED_TABLES:
            other = (
                await s.execute(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": B}
                )
            ).scalar_one()
            assert other == 0, table
    async with tenant_session(app_engine, [B]) as s:
        assert [t.name for t in (await load_snapshot(s)).tenants] == ["globex"]


async def test_no_scope_sees_nothing_and_multi_scope_sees_both(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    await seed_both(admin_engine)
    async with tenant_session(app_engine, []) as s:
        snap = await load_snapshot(s)
        assert snap.tenants == ()
        assert snap.virtual_services == ()
    async with tenant_session(app_engine, [A, B]) as s:
        assert len((await load_snapshot(s)).tenants) == 2
    async with app_engine.begin() as conn:  # the setting was never set at all
        assert (await conn.execute(text("SELECT count(*) FROM virtual_service"))).scalar_one() == 0


async def test_tenant_cannot_update_or_delete_other_tenants_rows(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    await seed_both(admin_engine)
    async with tenant_session(app_engine, [A]) as s:
        for table in SCOPED_TABLES:
            res = await s.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": B})
            assert res.rowcount == 0, table  # type: ignore[attr-defined]
        res = await s.execute(
            text("UPDATE virtual_service SET enabled = false WHERE tenant_id = :t"), {"t": B}
        )
        assert res.rowcount == 0  # type: ignore[attr-defined]
        res = await s.execute(text("UPDATE tenant SET name = 'pwned' WHERE id = :t"), {"t": B})
        assert res.rowcount == 0  # type: ignore[attr-defined]
    async with admin_session(admin_engine) as s:
        enabled = await s.execute(text("SELECT count(*) FROM virtual_service WHERE enabled"))
        assert enabled.scalar_one() == 2
        names = (await s.execute(text("SELECT name FROM tenant ORDER BY name"))).scalars().all()
        assert names == ["acme", "globex"]


async def test_tenant_cannot_insert_rows_for_other_tenants(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    await seed_both(admin_engine)
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(app_engine, [A]) as s:
            await s.execute(
                text(
                    "INSERT INTO network (id, tenant_id, name, cidr)"
                    " VALUES (:i, :t, 'evil', '198.51.100.0/24')"
                ),
                {"i": uuid.uuid4(), "t": B},
            )


async def test_tenant_cannot_move_a_row_to_another_tenant(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    await seed_both(admin_engine)
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_session(app_engine, [A]) as s:
            await s.execute(
                text("UPDATE network SET tenant_id = :b WHERE tenant_id = :a"), {"a": A, "b": B}
            )


async def test_app_role_cannot_create_or_delete_tenants(app_engine: AsyncEngine) -> None:
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(app_engine, [uuid.uuid4()]) as s:
            await s.execute(text("INSERT INTO tenant (id, name) VALUES (gen_random_uuid(), 'x')"))
    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_session(app_engine, [uuid.uuid4()]) as s:
            await s.execute(text("DELETE FROM tenant"))


async def test_composite_foreign_keys_block_cross_tenant_references_even_for_admin(
    admin_engine: AsyncEngine,
) -> None:
    a, b = tenant_data("a", "acme"), tenant_data("b", "globex")
    b["virtual_services"][0]["default_pool_id"] = "pool-web-a"  # tenant B -> tenant A's pool
    async with admin_session(admin_engine) as s:
        await add_snapshot(s, snapshot_from_mapping(a))
    with pytest.raises(IntegrityError, match="foreign key"):
        async with admin_session(admin_engine) as s:
            await add_snapshot(s, snapshot_from_mapping(b))


async def test_names_are_unique_per_tenant_not_globally(admin_engine: AsyncEngine) -> None:
    a, b = tenant_data("a", "acme"), tenant_data("b", "globex")
    assert a["networks"][0]["name"] == b["networks"][0]["name"]
    await seed_both(admin_engine)  # same names in two tenants is fine
    dup: dict[str, Any] = {
        "networks": [{"id": "dup", "tenant_id": "t-acme-a", "name": "lb", "cidr": "203.0.113.0/24"}]
    }
    with pytest.raises(IntegrityError, match="uq_network_tenant_id_name"):
        async with admin_session(admin_engine) as s:
            await add_snapshot(s, snapshot_from_mapping(dup))


async def test_version_must_be_positive(admin_engine: AsyncEngine) -> None:
    await seed_both(admin_engine)
    with pytest.raises(IntegrityError, match="ck_network_version"):
        async with admin_session(admin_engine) as s:
            await s.execute(text("UPDATE network SET version = 0"))


async def test_tenant_scoped_session_can_write_its_own_rows(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    await seed_both(admin_engine)
    async with tenant_session(app_engine, [A]) as s:
        res = await s.execute(text("UPDATE virtual_service SET enabled = false"))
        assert res.rowcount == 1  # type: ignore[attr-defined]
    async with tenant_session(app_engine, [A]) as s:
        assert (await load_snapshot(s)).virtual_services[0].enabled is False
    async with tenant_session(app_engine, [B]) as s:
        assert (await load_snapshot(s)).virtual_services[0].enabled is True


async def test_garbage_scope_fails_closed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    await seed_both(admin_engine)
    async with app_engine.connect() as conn:
        await conn.execute(text("SELECT set_config('app.tenant_ids', 'not-a-uuid', false)"))
        with pytest.raises(DBAPIError, match="invalid input syntax for type uuid"):
            await conn.execute(text("SELECT count(*) FROM network"))
