"""Managed IP lists end to end: sources become allow-list policy sets through a change set."""

from typing import Any
from uuid import UUID

import pytest

from switchyard.adapters.iplists import FakeIpLists
from switchyard.domain import models as dm
from switchyard.domain.loader import symbolic_uuid
from switchyard.domain.workflow import ChangeSetState
from switchyard.services.iplists import ACTOR, IpListSync
from tests.pipeline_env import TENANT, World, principal, world, world_data

pytestmark = pytest.mark.usefixtures("owner_engine")

GEO = UUID(symbolic_uuid("ps-geo"))
REP = UUID(symbolic_uuid("ps-rep"))
CF = UUID(symbolic_uuid("ps-cf"))


def data(*, geo_cidrs: list[str] | None = None) -> dict[str, Any]:
    d = world_data()
    d["policy_sets"] = [
        {
            "id": "ps-geo",
            "tenant_id": "t-acme",
            "name": "blocked-countries",
            "kind": "allow_list",
            "managed_by": "geo",
            "countries": ["XX", "YY"],
            "cidrs": geo_cidrs or [],
        },
        {
            "id": "ps-rep",
            "tenant_id": "t-acme",
            "name": "bad-reputation",
            "kind": "allow_list",
            "managed_by": "reputation",
            "feed": "drop",
            "cidrs": ["203.0.113.0/24"],
        },
        {
            "id": "ps-cf",
            "tenant_id": "t-acme",
            "name": "cloudflare",
            "kind": "allow_list",
            "managed_by": "cloudflare",
            "cidrs": ["198.51.100.0/24"],
        },
    ]
    return d


def source() -> FakeIpLists:
    return FakeIpLists(
        countries={"XX": ["192.0.2.0/25"], "YY": ["192.0.2.128/25", "192.0.2.0/25"]},
        feed_lists={"drop": ["203.0.113.0/24", "203.0.114.0/24"]},
    )


async def sync(w: World, lists: FakeIpLists) -> int:
    changed = await IpListSync(w.db, lists, w.service).run_tenant(TENANT)
    for record in await w.service.list_change_sets(
        principal(), TENANT, states={ChangeSetState.APPLYING}
    ):
        await w.pipeline.run(TENANT, record.id)
    return changed


async def cidrs(w: World, policy_id: UUID) -> list[str]:
    policy = next(p for p in (await w.snapshot()).policy_sets if p.id == policy_id)
    assert isinstance(policy, dm.AllowListPolicySet)
    return [str(c) for c in policy.cidrs]


async def test_lists_are_filled_from_their_sources_and_cloudflare_is_left_alone(
    pg_url: str,
) -> None:
    async with world(pg_url, data()) as w:
        lists = source()
        assert await sync(w, lists) == 2
        assert await cidrs(w, GEO) == ["192.0.2.0/25", "192.0.2.128/25"]  # union, no duplicates
        assert await cidrs(w, REP) == ["203.0.113.0/24", "203.0.114.0/24"]
        assert await cidrs(w, CF) == ["198.51.100.0/24"]
        assert ("country", "XX") in lists.calls and ("feed", "drop") in lists.calls
        assert await sync(w, lists) == 0  # nothing differs, so no change set


async def test_a_failing_or_empty_source_keeps_the_list(pg_url: str) -> None:
    async with world(pg_url, data(geo_cidrs=["192.0.2.0/24"])) as w:
        lists = source()
        lists.broken = {"XX"}
        lists.feed_lists["drop"] = []
        assert await sync(w, lists) == 0
        assert await cidrs(w, GEO) == ["192.0.2.0/24"]
        assert await cidrs(w, REP) == ["203.0.113.0/24"]


async def test_a_list_that_would_collapse_is_kept(pg_url: str) -> None:
    big = [f"10.{i}.0.0/16" for i in range(100)]
    d = data()
    d["policy_sets"][1]["cidrs"] = big
    async with world(pg_url, d) as w:
        lists = source()
        lists.feed_lists["drop"] = ["203.0.113.0/24"]  # a truncated download
        assert await sync(w, lists) == 1  # only the geo list changes
        assert len(await cidrs(w, REP)) == 100


async def test_a_waiting_change_set_blocks_the_next_run(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        service = IpListSync(w.db, source(), w.service)
        assert await service.run_tenant(TENANT) == 2  # queued, not applied yet
        assert await service.run_tenant(TENANT) == 0
        records = await w.service.list_change_sets(principal(), TENANT)
        assert [r.author for r in records].count(ACTOR) == 1


async def test_tenants_without_managed_lists_cost_no_fetch(pg_url: str) -> None:
    async with world(pg_url) as w:
        lists = source()
        assert await IpListSync(w.db, lists, w.service).run() == {}
        assert lists.calls == []


async def test_an_unknown_feed_is_skipped_not_fatal(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        lists = source()
        lists.feed_lists.clear()
        assert await sync(w, lists) == 1
        assert await cidrs(w, REP) == ["203.0.113.0/24"]
