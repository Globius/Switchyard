"""Pool discovery end to end: guests in a fake Proxmox become members through a change set."""

from typing import Any
from uuid import UUID

import pytest

from switchyard.adapters.proxmox import FakeProxmox, GuestInfo
from switchyard.domain.changes import Action, ChangeOp, ObjectType
from switchyard.domain.loader import symbolic_uuid
from switchyard.domain.workflow import ChangeSetState
from switchyard.services.discovery import ACTOR, DiscoveryService, member_id
from tests.pipeline_env import TENANT, World, principal, world, world_data

pytestmark = pytest.mark.usefixtures("owner_engine")

TAG = "sy-acme.web"
POOL = UUID(symbolic_uuid("pool-web"))


def data() -> dict[str, Any]:
    d = world_data()
    d["pools"][0]["discovery"] = {"tag": TAG, "cidr": "10.20.0.0/24", "port": 8080}
    return d


def guest(vmid: int, status: str = "running", tag: str = TAG) -> GuestInfo:
    return GuestInfo(vmid, "pve1", f"web-{vmid}", status, "qemu", (tag,))


async def run_and_apply(w: World, pve: FakeProxmox) -> int:
    """One discovery run; the queued change set is applied through the pipeline."""
    changed = await DiscoveryService(w.db, pve, w.service).run_tenant(TENANT)
    for record in await w.service.list_change_sets(
        principal(), TENANT, states={ChangeSetState.APPLYING}
    ):
        await w.pipeline.run(TENANT, record.id)
    return changed


async def members(w: World) -> dict[str, tuple[str, int, str]]:
    pool = next(p for p in (await w.snapshot()).pools if p.id == POOL)
    return {m.server_name: (str(m.address), m.weight, m.admin_state.value) for m in pool.members}


async def test_guests_become_members_and_the_next_run_changes_nothing(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        pve = FakeProxmox(
            guests=[guest(101), guest(102), guest(103, tag="other")],
            guest_ips={101: ("10.20.0.11",), 102: ("10.20.0.12",), 103: ("10.20.0.13",)},
        )
        assert await run_and_apply(w, pve) == 1
        assert await members(w) == {
            "pve-101": ("10.20.0.11", 100, "enabled"),
            "pve-102": ("10.20.0.12", 100, "enabled"),
        }
        pool = next(p for p in (await w.snapshot()).pools if p.id == POOL)
        assert {m.id for m in pool.members} == {member_id(POOL, 101), member_id(POOL, 102)}
        assert await run_and_apply(w, pve) == 0  # nothing differs, so no change set


async def test_changes_follow_the_cluster_and_keep_operator_settings(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        pve = FakeProxmox(
            guests=[guest(101), guest(102)],
            guest_ips={101: ("10.20.0.11",), 102: ("10.20.0.12",)},
        )
        await run_and_apply(w, pve)
        # An operator drains 101 and halves its weight through a normal change.
        pool = next(p for p in (await w.snapshot()).pools if p.id == POOL)
        tuned = [
            {**m.model_dump(mode="json"), "weight": 50, "admin_state": "drain"}
            if m.server_name == "pve-101"
            else m.model_dump(mode="json")
            for m in pool.members
        ]
        await w.run_change(
            [
                ChangeOp(
                    object_type=ObjectType.POOL,
                    object_id=POOL,
                    action=Action.UPDATE,
                    body={"members": tuned},
                    expected_version=pool.version,
                )
            ]
        )
        # 102 stops, 104 appears, 101 moves address.
        pve.guests = [guest(101), guest(102, "stopped"), guest(104)]
        pve.guest_ips = {101: ("10.20.0.21",), 104: ("10.20.0.14",)}
        assert await run_and_apply(w, pve) == 1
        assert await members(w) == {
            "pve-101": ("10.20.0.21", 50, "drain"),
            "pve-104": ("10.20.0.14", 100, "enabled"),
        }


async def test_a_run_never_empties_a_pool(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        pve = FakeProxmox(guests=[guest(101)], guest_ips={101: ("10.20.0.11",)})
        await run_and_apply(w, pve)
        pve.guests = []  # Proxmox answers, but with nothing
        assert await run_and_apply(w, pve) == 0
        assert list(await members(w)) == ["pve-101"]


async def test_a_waiting_discovery_change_set_blocks_a_second_one(pg_url: str) -> None:
    async with world(pg_url, data()) as w:
        pve = FakeProxmox(guests=[guest(101)], guest_ips={101: ("10.20.0.11",)})
        service = DiscoveryService(w.db, pve, w.service)
        assert await service.run_tenant(TENANT) == 1  # queued, not yet applied
        pve.guests = [guest(101), guest(102)]
        pve.guest_ips = {101: ("10.20.0.11",), 102: ("10.20.0.12",)}
        assert await service.run_tenant(TENANT) == 0
        records = await w.service.list_change_sets(principal(), TENANT)
        assert [r.author for r in records if r.author == ACTOR] == [ACTOR]


async def test_tenants_without_discovery_pools_cost_no_proxmox_call(pg_url: str) -> None:
    async with world(pg_url) as w:
        pve = FakeProxmox()
        assert await DiscoveryService(w.db, pve, w.service).run() == {}
        assert pve.calls == []
