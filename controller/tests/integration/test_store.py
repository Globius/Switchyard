"""Writing a change-set result to PostgreSQL."""

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.db.repository import add_snapshot, load_snapshot
from switchyard.db.store import ConcurrentModificationError, apply_delta, compute_delta
from switchyard.db.tenancy import admin_session
from switchyard.domain.changes import Action, ChangeOp, ObjectType, apply_ops
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.domain.snapshot import Snapshot
from tests.factories import base_data

TENANT = uuid.UUID(symbolic_uuid("t-acme"))
POOL = uuid.UUID(symbolic_uuid("pool-web"))
VS = uuid.UUID(symbolic_uuid("vs-web"))


def op(action: Action, object_type: ObjectType, object_id: uuid.UUID, **kw: Any) -> ChangeOp:
    return ChangeOp(object_type=object_type, object_id=object_id, action=action, **kw)


def canonical(snapshot: Snapshot) -> dict[str, Any]:
    out = snapshot.model_dump(mode="json")
    for key, objects in out.items():
        out[key] = sorted(objects, key=lambda o: (o.get("name", ""), o["id"]))
        for obj in out[key]:
            for child in ("ports", "routes", "members"):
                if child in obj:
                    obj[child] = sorted(obj[child], key=str)
    return out


async def seed(engine: AsyncEngine) -> Snapshot:
    snap = snapshot_from_mapping(base_data())
    async with admin_session(engine) as s:
        await add_snapshot(s, snap)
    return snap


async def test_mixed_create_update_delete_is_written_in_a_valid_order(
    admin_engine: AsyncEngine,
) -> None:
    base = await seed(admin_engine)
    new_pool, new_vs = uuid.UUID(int=11), uuid.UUID(int=12)
    ops = [
        op(
            Action.CREATE,
            ObjectType.POOL,
            new_pool,
            body={
                "name": "api",
                "members": [{"id": str(uuid.UUID(int=13)), "address": "10.20.0.60", "port": 9000}],
            },
        ),
        op(
            Action.CREATE,
            ObjectType.VIRTUAL_SERVICE,
            new_vs,
            body={
                "name": "api",
                "se_group_id": str(uuid.UUID(symbolic_uuid("seg-1"))),
                "protocol": "http",
                "vip": "192.0.2.25",
                "ports": [{"port": 9000}],
                "default_pool_id": str(new_pool),
            },
        ),
        # repoint the existing service at the new pool, then delete the old pool
        op(
            Action.UPDATE,
            ObjectType.VIRTUAL_SERVICE,
            VS,
            body={
                "routes": [{"match": {"host": "app.example.test"}, "pool_id": str(new_pool)}],
                "ports": [{"port": 443, "tls": True}],
            },
            expected_version=1,
        ),
        op(Action.DELETE, ObjectType.POOL, POOL, expected_version=1),
    ]
    draft = apply_ops(base, ops, tenant_id=TENANT)
    assert draft.problems == ()
    async with admin_session(admin_engine) as s:
        delta = await apply_delta(s, base, draft.snapshot)
    assert not delta.is_empty
    async with admin_session(admin_engine) as s:
        stored = await load_snapshot(s)
    assert canonical(stored) == canonical(draft.snapshot)
    web = next(v for v in stored.virtual_services if v.name == "web")
    assert (web.version, [p.port for p in web.ports]) == (2, [443])


async def test_stale_version_is_detected_at_write_time(admin_engine: AsyncEngine) -> None:
    base = await seed(admin_engine)
    ops = [op(Action.UPDATE, ObjectType.POOL, POOL, body={"tls": True}, expected_version=1)]
    draft = apply_ops(base, ops, tenant_id=TENANT)
    async with admin_session(admin_engine) as s:  # somebody else changed the pool meanwhile
        await s.execute(text("UPDATE pool SET version = 2 WHERE id = :i"), {"i": POOL})
    with pytest.raises(ConcurrentModificationError):
        async with admin_session(admin_engine) as s:
            await apply_delta(s, base, draft.snapshot)
    async with admin_session(admin_engine) as s:  # the failed transaction wrote nothing
        assert (await load_snapshot(s)).pools[0].tls is False


async def test_stale_delete_is_detected(admin_engine: AsyncEngine) -> None:
    base = await seed(admin_engine)
    draft = apply_ops(
        base,
        [
            op(Action.DELETE, ObjectType.VIRTUAL_SERVICE, VS, expected_version=1),
        ],
        tenant_id=TENANT,
    )
    async with admin_session(admin_engine) as s:
        await s.execute(text("UPDATE virtual_service SET version = 5"))
    with pytest.raises(ConcurrentModificationError):
        async with admin_session(admin_engine) as s:
            await apply_delta(s, base, draft.snapshot)


async def test_children_are_replaced_not_duplicated(admin_engine: AsyncEngine) -> None:
    base = await seed(admin_engine)
    members = [
        {"id": str(uuid.UUID(int=21)), "address": "10.20.0.31", "port": 80},
        {"id": str(uuid.UUID(int=22)), "address": "10.20.0.32", "port": 80},
    ]
    ops = [op(Action.UPDATE, ObjectType.POOL, POOL, body={"members": members}, expected_version=1)]
    draft = apply_ops(base, ops, tenant_id=TENANT)
    async with admin_session(admin_engine) as s:
        await apply_delta(s, base, draft.snapshot)
    async with admin_session(admin_engine) as s:
        count = (await s.execute(text("SELECT count(*) FROM pool_member"))).scalar_one()
        assert count == 2
        stored = await load_snapshot(s)
    assert [str(m.address) for m in stored.pools[0].members] == ["10.20.0.31", "10.20.0.32"]


async def test_unchanged_snapshots_write_nothing(admin_engine: AsyncEngine) -> None:
    base = await seed(admin_engine)
    assert compute_delta(base, base).is_empty
    async with admin_session(admin_engine) as s:
        delta = await apply_delta(s, base, base)
    assert delta.is_empty
