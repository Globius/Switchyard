# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The autoscaler's decision, its enrollment files and the guest-agent calls (ADR 0194)."""

import copy
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from switchyard.adapters.dataplane import _optional_int
from switchyard.adapters.proxmox import FakeProxmox, HttpProxmox, ProxmoxError, VmSpec
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.models import Autoscale
from switchyard.domain.snapshot import validate_snapshot
from switchyard.services.autoscale import AGENT_DIR, decide, enrollment_files, utilization
from tests.factories import BASE

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
ENROLL = {"controller_url": "https://ctrl.example.test", "ca_ref": "kv/ca"}


def policy(**extra: Any) -> Autoscale:
    base = {
        "deploy": {
            "nodes": ["pve1"],
            "template_vmid": 9000,
            "mgmt_pool_id": str(UUID(int=1)),
            "credentials_ref": "kv/se/dpapi",
        },
        "enroll": ENROLL,
    }
    return Autoscale.model_validate(base | extra)


def verdict(p: Autoscale, **kw: Any) -> str:
    args: dict[str, Any] = {
        "now": NOW,
        "util": 90,
        "engines": 2,
        "group_score": 100,
        "busy_since": NOW - timedelta(hours=1),
        "last_scaled_at": None,
    } | kw
    return decide(p, **args).state


def test_utilization_is_the_mean_of_each_engines_share_of_its_own_limit() -> None:
    assert utilization([(500, 1000), (100, 200)]) == 50
    assert utilization([(0, 0)]) is None


def test_below_the_threshold_is_idle_and_forgets_when_it_got_busy() -> None:
    result = decide(
        policy(),
        now=NOW,
        util=69,
        engines=2,
        group_score=100,
        busy_since=NOW - timedelta(hours=1),
        last_scaled_at=None,
    )
    assert (result.state, result.busy_since) == ("idle", None)
    assert verdict(policy(), util=None) == "idle"  # no load data is not a reason to scale


def test_the_order_of_the_gates() -> None:
    p = policy(max_engines=4, cooldown_s=900)
    assert verdict(p) == "scale_out"
    assert verdict(p, busy_since=NOW - timedelta(seconds=299)) == "busy"
    assert verdict(p, busy_since=None) == "busy"  # first sight of the load starts the clock
    assert verdict(p, engines=4) == "at_max"
    assert verdict(p, last_scaled_at=NOW - timedelta(seconds=899)) == "cooldown"
    assert verdict(p, last_scaled_at=NOW - timedelta(seconds=901)) == "scale_out"
    assert verdict(p, group_score=79) == "blocked"
    assert verdict(p, group_score=80) == "scale_out"


def test_the_enrollment_files_match_what_the_quickstart_script_writes() -> None:
    files = enrollment_files(
        policy(enroll={"controller_url": "https://ctrl.example.test:8443/", "ca_ref": "kv/ca"}),
        token="syenr_abc",
        data_plane_login="switchyard:p:w",  # only the first colon separates user and password
        ca_pem="PEM\n\n",
    )
    assert files[f"{AGENT_DIR}/enroll.env"] == (
        "CONTROLLER_URL=https://ctrl.example.test:8443\n"
        "CONTROLLER_CN=switchyard-controller\n"
        "DP_USER=switchyard\nDP_PASSWORD=p:w\n",
        "600",
    )
    assert files[f"{AGENT_DIR}/enroll.token"] == ("syenr_abc\n", "600")
    assert files[f"{AGENT_DIR}/controller-ca.crt"] == ("PEM\n", "644")


def test_the_policy_rejects_unsafe_or_silly_values() -> None:
    for bad in (
        {"max_engines": 1},
        {"scale_out_at": 10},
        {"sustain_s": 5},
        {"enroll": {"controller_url": "http://ctrl.example.test", "ca_ref": "kv/ca"}},
    ):
        with pytest.raises(ValidationError):
            policy(**bad)


def test_the_data_plane_load_keys() -> None:
    assert _optional_int({"curr_conns": 7}, "curr_conns", "CurrConns") == 7
    assert _optional_int({"CurrConns": "9"}, "curr_conns", "CurrConns") == 9
    assert _optional_int({"curr_conns": "x"}, "curr_conns") is None
    assert _optional_int({}, "curr_conns") is None


def autoscaled(mgmt: bool = True, **deploy: Any) -> list[tuple[str, str]]:
    data = copy.deepcopy(BASE)
    data["networks"].append(
        {"id": "net-mgmt", "tenant_id": "t-acme", "name": "mgmt", "cidr": "10.30.0.0/24"}
    )
    data["vip_pools"].append(
        {
            "id": "vp-mgmt",
            "tenant_id": "t-acme",
            "name": "mgmt",
            "network_id": "net-mgmt",
            "ranges": [{"start": "10.30.0.10", "end": "10.30.0.20"}],
        }
    )
    if mgmt:
        data["se_groups"][0]["networks"].append({"network_id": "net-mgmt", "role": "mgmt"})
    data["se_groups"][0]["autoscale"] = {
        "deploy": {
            "nodes": ["pve1"],
            "template_vmid": 9000,
            "mgmt_pool_id": "vp-mgmt",
            "credentials_ref": "kv/se/dpapi",
        }
        | deploy,
        "enroll": ENROLL,
    }
    for net in data["networks"]:
        net.setdefault("vnet", f"v{net['name']}")
    return [(p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(data))]


def test_a_well_formed_autoscaled_group_validates() -> None:
    assert autoscaled() == []


def test_the_deploy_pool_must_be_on_the_mgmt_network() -> None:
    found = autoscaled(mgmt_pool_id="vp-main")
    assert ("se_groups.edge.autoscale.deploy.mgmt_pool_id", "autoscale_pool_mismatch") in found


def test_an_autoscaled_group_needs_a_mgmt_network() -> None:
    found = autoscaled(mgmt=False)
    assert ("se_groups.edge.autoscale", "autoscale_needs_mgmt_network") in found


class GuestApi:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, list[str]]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api2/json")
        self.requests.append((request.method, path, parse_qs(request.content.decode())))
        if path.startswith("/nodes/pve1/qemu/105/agent/"):
            return httpx.Response(200, json={"data": {"pid": 5}})
        return httpx.Response(500, text="QEMU guest agent is not running")


async def test_files_and_commands_go_through_the_proxmox_guest_agent() -> None:
    api = GuestApi()
    pve = HttpProxmox(
        "https://pve.invalid:8006",
        "sy@pve!ctl",
        "secret",
        transport=httpx.MockTransport(api.handler),
    )
    await pve.guest_write_file("pve1", 105, f"{AGENT_DIR}/enroll.token", "tok\n")
    await pve.guest_exec("pve1", 105, ["chmod", "600", f"{AGENT_DIR}/enroll.token"])
    assert api.requests[0] == (
        "POST",
        "/nodes/pve1/qemu/105/agent/file-write",
        {"file": [f"{AGENT_DIR}/enroll.token"], "content": ["tok\n"]},
    )
    assert api.requests[1] == (
        "POST",
        "/nodes/pve1/qemu/105/agent/exec",
        {"command": ["chmod", "600", f"{AGENT_DIR}/enroll.token"]},
    )
    with pytest.raises(ProxmoxError, match="guest agent is not running"):
        await pve.guest_exec("pve1", 999, ["true"])


async def test_the_fake_refuses_a_guest_that_is_not_running() -> None:
    pve = FakeProxmox()
    info = await pve.create_vm(
        VmSpec(name="a", node="pve1", template_vmid=9000, mgmt_ip_cidr="10.0.0.2/24")
    )
    with pytest.raises(ProxmoxError, match="guest agent"):
        await pve.guest_write_file("pve1", info.vmid, "/x", "y")
    await pve.start_vm("pve1", info.vmid)
    await pve.guest_write_file("pve1", info.vmid, "/x", "y")
    assert pve.guest_files[(info.vmid, "/x")] == "y"
