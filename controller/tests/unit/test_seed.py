"""Tests for the demo data and id reassignment (no database needed)."""

import uuid

from switchyard.domain.loader import reassign_ids, snapshot_from_mapping
from switchyard.domain.snapshot import validate_snapshot
from switchyard.ids import uuid7
from switchyard.render.haproxy import render_haproxy
from switchyard.services.seed import example_snapshot
from tests.factories import base_data


def test_example_is_valid_and_uses_uuid7() -> None:
    snap = example_snapshot()
    assert validate_snapshot(snap) == []
    ids = [snap.tenants[0].id, snap.virtual_services[0].id, snap.pools[0].members[0].id]
    assert all(i.version == 7 for i in ids)


def test_example_renders_both_service_kinds() -> None:
    snap = example_snapshot()
    result = render_haproxy(snap, snap.se_groups[0].id)
    cfg = result.files["haproxy.cfg"]
    assert "frontend vs_web" in cfg
    assert "frontend vs_k8s-api" in cfg
    assert sorted(result.files) == [
        "crt-lists/web.crtlist",
        "haproxy.cfg",
        "maps/web.host.map",
    ]


def test_reassign_ids_rewrites_references_consistently() -> None:
    original = snapshot_from_mapping(base_data())
    counter = iter(range(1, 1000))
    fresh = reassign_ids(original, lambda: uuid.UUID(int=next(counter)))
    assert validate_snapshot(fresh) == []
    assert {n.id for n in fresh.networks}.isdisjoint({n.id for n in original.networks})
    assert fresh.virtual_services[0].default_pool_id is None
    assert fresh.virtual_services[0].routes[0].pool_id == fresh.pools[0].id
    assert fresh.se_groups[0].networks[0].network_id == fresh.networks[0].id


def test_render_does_not_depend_on_ids() -> None:
    a = reassign_ids(example_snapshot(), uuid7)
    b = reassign_ids(example_snapshot(), uuid7)
    ra = render_haproxy(a, a.se_groups[0].id)
    rb = render_haproxy(b, b.se_groups[0].id)
    assert ra.content_hash == rb.content_hash
