# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pool discovery by Proxmox tag (ADR 0145): the rule, the member computation and the REST calls."""

import copy
import ipaddress
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from switchyard.adapters.proxmox import FakeProxmox, GuestInfo, HttpProxmox, parse_tags
from switchyard.domain import models as dm
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import validate_snapshot
from switchyard.domain.validators import discovery_tag_prefix, validate_discovery_tag
from switchyard.services.discovery import desired_members, member_id, pick_address
from tests.factories import BASE

POOL_ID = UUID(int=7)
TAG = "sy-acme.web"


def pool(**kw: Any) -> dm.Pool:
    discovery = dm.PoolDiscovery(tag=TAG, cidr="10.20.0.0/24", port=8080)
    return dm.Pool(id=POOL_ID, tenant_id=UUID(int=1), name="web", discovery=discovery, **kw)


def guest(vmid: int, status: str = "running", tags: tuple[str, ...] = (TAG,)) -> GuestInfo:
    return GuestInfo(vmid, "pve1", f"web-{vmid}", status, "qemu", tags)


# -- validators and the tenant rule ---------------------------------------------------------------


@pytest.mark.parametrize("tag", ["sy-acme.web", "a", "web_1", "a+b.c-d"])
def test_tags_proxmox_accepts_pass(tag: str) -> None:
    assert validate_discovery_tag(tag) == tag


@pytest.mark.parametrize("tag", ["", "Web", "a b", "a;b", "-x", "x" * 65, "a/b", "a,b"])
def test_other_tags_are_refused(tag: str) -> None:
    with pytest.raises(ValueError, match="tag must be"):
        validate_discovery_tag(tag)


def discovery_data(tag: str, tenant: str = "acme") -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    d["tenants"][0]["name"] = tenant
    d["pools"][0]["discovery"] = {"tag": tag, "cidr": "10.20.0.0/24", "port": 8080}
    return d


def codes(d: dict[str, Any]) -> list[str]:
    return [p.code for p in validate_snapshot(snapshot_from_mapping(d))]


def test_a_tenant_may_discover_its_own_prefix() -> None:
    assert "discovery_tag_not_allowed" not in codes(discovery_data("sy-acme.web"))


@pytest.mark.parametrize("tag", ["web", "sy-other.web", "sy-acme.", "sy-acme-x.web", "sy-acme"])
def test_a_tenant_may_not_discover_foreign_or_bare_tags(tag: str) -> None:
    assert "discovery_tag_not_allowed" in codes(discovery_data(tag))


def test_the_prefix_cannot_be_borrowed_by_a_tenant_with_a_longer_name() -> None:
    # tenant "acme-x" tags its guests sy-acme-x.<label>; tenant "acme" must not reach them
    assert discovery_tag_prefix("acme") == "sy-acme."
    assert not "sy-acme-x.web".startswith(discovery_tag_prefix("acme"))
    assert "discovery_tag_not_allowed" not in codes(discovery_data("sy-acme-x.web", "acme-x"))


def test_discovery_rejects_unknown_fields_and_other_sources() -> None:
    with pytest.raises(ValidationError):
        dm.PoolDiscovery(tag=TAG, cidr="10.0.0.0/24", port=80, source="consul")
    with pytest.raises(ValidationError):
        dm.PoolDiscovery(tag=TAG, cidr="10.0.0.0/24", port=80, script="x")  # type: ignore[call-arg]


# -- member computation ---------------------------------------------------------------------------


def test_pick_address_takes_the_first_one_in_the_network() -> None:
    net = ipaddress.ip_network("10.20.0.0/24")
    assert pick_address(("fe80::1", "192.0.2.5", "10.20.0.9", "10.20.0.10"), net) == "10.20.0.9"
    assert pick_address(("not-an-ip", "10.40.0.1"), net) is None
    assert pick_address(("10.20.0.9",), ipaddress.ip_network("2001:db8::/64")) is None


def test_members_come_from_running_tagged_guests_in_id_order() -> None:
    guests = [guest(102), guest(101), guest(103, "stopped"), guest(104, tags=("other",))]
    addresses: dict[int, tuple[str, ...] | None] = {
        101: ("10.20.0.11",),
        102: ("10.20.0.12",),
        103: ("10.20.0.13",),
    }
    members = desired_members(pool(), guests, addresses)
    assert [(m["name"], m["address"], m["port"]) for m in members] == [
        ("pve-101", "10.20.0.11", 8080),
        ("pve-102", "10.20.0.12", 8080),
    ]
    assert members[0]["id"] == str(member_id(POOL_ID, 101))


def test_a_guest_outside_the_network_is_not_a_member() -> None:
    assert desired_members(pool(), [guest(101)], {101: ("192.0.2.9",)}) == []


def test_operator_weight_and_state_survive() -> None:
    known = dm.PoolMember(
        id=member_id(POOL_ID, 101),
        address="10.20.0.11",
        port=8080,
        weight=30,
        admin_state=dm.AdminState.DRAIN,
        name="pve-101",
    )
    members = desired_members(pool(members=(known,)), [guest(101)], {101: ("10.20.0.11",)})
    assert members[0]["weight"] == 30 and members[0]["admin_state"] == "drain"


def test_a_guest_that_cannot_report_keeps_its_member_but_is_not_added() -> None:
    known = dm.PoolMember(
        id=member_id(POOL_ID, 101),
        address="10.20.0.11",
        port=8080,
        name="pve-101",
    )
    members = desired_members(
        pool(members=(known,)), [guest(101), guest(102)], {101: None, 102: None}
    )
    assert [m["name"] for m in members] == ["pve-101"]


def test_a_pool_without_discovery_has_no_desired_members() -> None:
    plain = dm.Pool(id=POOL_ID, tenant_id=UUID(int=1), name="web")
    assert desired_members(plain, [guest(101)], {101: ("10.20.0.11",)}) == []


# -- adapter --------------------------------------------------------------------------------------


def test_tags_are_split_and_lowered() -> None:
    assert parse_tags("sy-acme.web;Prod,db") == ("sy-acme.web", "prod", "db")
    assert parse_tags(5) == ()


RESOURCES = [
    {"type": "qemu", "vmid": 101, "node": "pve1", "name": "w1", "status": "running", "tags": TAG},
    {"type": "lxc", "vmid": 102, "node": "pve2", "name": "w2", "status": "running", "tags": TAG},
    {"type": "qemu", "vmid": 9000, "node": "pve1", "name": "tpl", "template": 1, "tags": TAG},
    {"type": "qemu", "vmid": 103, "node": "pve1", "name": "other", "tags": "db"},
    {"type": "storage", "id": "storage/pve1/local"},
]


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/api2/json")
    if path == "/cluster/resources":
        assert request.url.params["type"] == "vm"
        return httpx.Response(200, json={"data": RESOURCES})
    if path == "/nodes/pve1/qemu/101/agent/network-get-interfaces":
        nics = [
            {"name": "lo", "ip-addresses": [{"ip-address": "127.0.0.1"}]},
            {
                "name": "eth0",
                "ip-addresses": [{"ip-address": "10.20.0.11"}, {"ip-address": "fe80::1"}],
            },
        ]
        return httpx.Response(200, json={"data": {"result": nics}})
    if path == "/nodes/pve2/lxc/102/interfaces":
        return httpx.Response(
            200, json={"data": [{"name": "eth0", "inet": "10.20.0.12/24", "inet6": "fe80::2/64"}]}
        )
    if path.endswith("/agent/network-get-interfaces"):
        return httpx.Response(500, text="QEMU guest agent is not running")
    return httpx.Response(404)


@pytest.fixture
def pve() -> HttpProxmox:
    return HttpProxmox(
        "https://pve.invalid:8006", "sy@pve!ctl", "s", transport=httpx.MockTransport(handler)
    )


async def test_guests_with_a_tag_are_listed_without_templates(pve: HttpProxmox) -> None:
    guests = await pve.list_guests(TAG)
    assert [(g.vmid, g.node, g.kind, g.status) for g in guests] == [
        (101, "pve1", "qemu", "running"),
        (102, "pve2", "lxc", "running"),
    ]
    assert [g.vmid for g in await pve.list_guests()] == [101, 102, 103]


async def test_qemu_addresses_come_from_the_guest_agent(pve: HttpProxmox) -> None:
    assert await pve.guest_addresses("pve1", 101, "qemu") == ("127.0.0.1", "10.20.0.11", "fe80::1")


async def test_container_addresses_come_from_its_interfaces(pve: HttpProxmox) -> None:
    assert await pve.guest_addresses("pve2", 102, "lxc") == ("10.20.0.12", "fe80::2")


async def test_a_guest_without_an_agent_is_unknown_not_empty(pve: HttpProxmox) -> None:
    assert await pve.guest_addresses("pve1", 555, "qemu") is None


async def test_the_fake_filters_by_tag() -> None:
    fake = FakeProxmox(guests=[guest(1), guest(2, tags=("x",))], guest_ips={1: ("10.0.0.1",)})
    assert [g.vmid for g in await fake.list_guests(TAG)] == [1]
    assert await fake.guest_addresses("pve1", 1, "qemu") == ("10.0.0.1",)
    assert await fake.guest_addresses("pve1", 2, "qemu") is None
