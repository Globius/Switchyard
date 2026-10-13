# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Routing renderers: golden files and behaviour."""

import copy
import json
import uuid
from typing import Any

import pytest
import yaml

from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.render.escape import RenderError
from switchyard.render.routing import EngineSlot, render_agent_bundle, render_frr, render_keepalived
from tests.routing_support import (
    GOLDEN_DIR,
    case_names,
    expected,
    render_case,
    update_requested,
    write_expected,
)

GROUP = uuid.UUID(symbolic_uuid("seg-1"))


def load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((GOLDEN_DIR / name / "input.yaml").read_text("utf-8"))
    data.pop("render")
    return data  # type: ignore[no-any-return]


@pytest.mark.parametrize("name", case_names())
def test_rendered_files_match_expected(name: str) -> None:
    rendered = render_case(name)
    if update_requested():
        write_expected(name, rendered)
    assert rendered == expected(name)


@pytest.mark.parametrize("name", case_names())
def test_rendering_is_deterministic(name: str) -> None:
    assert render_case(name) == render_case(name)


def slot(index: int = 0, router_id: str = "10.30.0.11") -> EngineSlot:
    return EngineSlot(f"se-{index}", index, router_id)


class TestVrrp:
    def conf(self, index: int) -> str:
        snap = snapshot_from_mapping(load("01-vrrp"))
        return render_keepalived(snap, GROUP, slot(index))["keepalived/keepalived.conf"]

    def test_priority_falls_with_the_rollout_position(self) -> None:
        assert "priority 150" in self.conf(0)
        assert "priority 140" in self.conf(1)
        assert "priority 100" in self.conf(5)

    def test_priority_never_drops_below_one(self) -> None:
        assert "priority 1\n" in self.conf(40)

    def test_vips_carry_the_network_prefix_and_interface(self) -> None:
        conf = self.conf(0)
        assert "192.0.2.20/24 dev eth1" in conf
        assert "192.0.2.21/24 dev eth1" in conf

    def test_failing_haproxy_lowers_priority_and_state_is_recorded_for_the_agent(self) -> None:
        conf = self.conf(0)
        assert "weight -60" in conf
        assert "http://127.0.0.1:8404/healthz" in conf
        assert "/run/switchyard/vrrp-sy_edge.state" in conf

    def test_dual_stack_gets_one_instance_per_family(self) -> None:
        data = load("01-vrrp")
        data["networks"][0]["cidr"] = "192.0.2.0/24"
        data["networks"].append(
            {"id": "net-lb6", "tenant_id": "t-acme", "name": "lb6", "cidr": "2001:db8:1::/64"}
        )
        data["vip_pools"].append(
            {
                "id": "vp-6",
                "tenant_id": "t-acme",
                "name": "v6",
                "network_id": "net-lb6",
                "ranges": [{"start": "2001:db8:1::10", "end": "2001:db8:1::50"}],
            }
        )
        data["se_groups"][0]["networks"].append({"network_id": "net-lb6", "role": "vip"})
        data["virtual_services"][1]["vip"] = "2001:db8:1::21"
        data["virtual_services"][1].pop("vip_pool_id", None)
        data["routing_profiles"][0]["static_routes"].append(
            {"prefix": "2001:db8:99::/48", "next_hop": "2001:db8:1::1"}
        )
        conf = render_keepalived(snapshot_from_mapping(data), GROUP, slot())[
            "keepalived/keepalived.conf"
        ]
        assert conf.count("vrrp_instance ") == 2
        assert "vrrp_instance sy_edge {" in conf
        v6 = conf[conf.index("vrrp_instance sy_edge_6 {") :]
        v4 = conf[: conf.index("vrrp_instance sy_edge_6 {")]
        assert "version 3" in v6 and "version 3" not in v4
        assert "2001:db8:1::21/64 dev eth1" in v6 and "2001:db8:1::21" not in v4
        assert "192.0.2.20/24 dev eth1" in v4 and "192.0.2.20" not in v6
        assert "2001:db8:99::/48 via 2001:db8:1::1 dev eth1" in v6
        assert "10.99.0.0/16 via 192.0.2.1 dev eth1" in v4
        assert "/run/switchyard/vrrp-sy_edge_6.state" in v6
        assert conf.count("virtual_router_id 51") == 2

    def test_wrong_mode_is_refused(self) -> None:
        snap = snapshot_from_mapping(load("02-bgp"))
        with pytest.raises(RenderError, match="does not use VRRP"):
            render_keepalived(snap, GROUP, slot())


class TestBgp:
    def render(
        self, secrets: dict[str, str] | None = None, router: str = "10.30.0.11"
    ) -> dict[str, str]:
        snap = snapshot_from_mapping(load("02-bgp"))
        return render_frr(snap, GROUP, slot(0, router), secrets or {"kv/bgp/peer-1": "pw-1"})

    def test_only_vips_are_announced_and_nothing_is_accepted(self) -> None:
        conf = self.render()["frr/frr.conf"]
        assert "network 192.0.2.20/32" in conf
        assert "route-map SY-OUT out" in conf
        assert "route-map SY-IN deny 10" in conf
        assert "route-map SY-IN in" in conf

    def test_bfd_password_and_ecmp(self) -> None:
        conf = self.render()["frr/frr.conf"]
        assert "neighbor 10.30.0.1 password pw-1" in conf
        assert "neighbor 10.30.0.2 password" not in conf
        assert conf.count(" bfd\n") == 2
        assert "maximum-paths 4" in conf

    def test_watchdog_file_lists_the_ipv4_vips(self) -> None:
        vips = json.loads(self.render()["frr/vips.json"])
        assert vips == {"asn": 65010, "prefixes": ["192.0.2.20/32", "192.0.2.21/32"]}
        assert "udp_prefixes" not in vips

    def test_missing_secret_fails_instead_of_dropping_authentication(self) -> None:
        with pytest.raises(RenderError, match="missing secret kv/bgp/peer-1"):
            self.render(secrets={"other": "x"})

    @pytest.mark.parametrize("bad", ["pw 1", "pw\nrouter bgp 1", 'pw"', "pw!", "", "x" * 81])
    def test_unsafe_passwords_are_refused(self, bad: str) -> None:
        with pytest.raises(RenderError):
            self.render(secrets={"kv/bgp/peer-1": bad})

    def test_ipv6_only_engine_gets_a_stable_derived_router_id(self) -> None:
        first = self.render(router="2001:db8::5")["frr/frr.conf"]
        again = self.render(router="2001:db8::9")["frr/frr.conf"]
        line = next(x for x in first.splitlines() if "bgp router-id" in x)
        assert line == next(x for x in again.splitlines() if "bgp router-id" in x)
        assert line.split()[-1].startswith("100.")

    def test_ipv4_management_address_stays_the_router_id(self) -> None:
        assert " bgp router-id 10.30.0.11" in self.render()["frr/frr.conf"]

    def test_the_password_appears_only_in_frr_conf(self) -> None:
        files = self.render()
        assert "pw-1" in files["frr/frr.conf"]
        assert "pw-1" not in files["frr/vips.json"]

    def test_dual_stack_activates_each_neighbour_in_its_family(self) -> None:
        snap = snapshot_from_mapping(load("03-bgp-dual-stack"))
        conf = render_frr(snap, GROUP, slot(), {})["frr/frr.conf"]
        v4, v6 = conf.split("address-family ipv6 unicast")
        assert "neighbor 10.30.0.1 activate" in v4
        assert "neighbor 2001:db8:30::1 activate" in v6
        assert "network 2001:db8:10::20/128" in v6
        assert "router bgp 4200000001" in conf


def test_bundle_follows_the_ha_mode() -> None:
    vrrp = snapshot_from_mapping(load("01-vrrp"))
    bgp = snapshot_from_mapping(load("02-bgp"))
    assert set(render_agent_bundle(vrrp, GROUP, slot(), {})) == {"keepalived/keepalived.conf"}
    assert set(render_agent_bundle(bgp, GROUP, slot(), {"kv/bgp/peer-1": "x"})) == {
        "frr/frr.conf",
        "frr/vips.json",
    }


def test_group_without_routing_profile_cannot_render() -> None:
    data = copy.deepcopy(load("01-vrrp"))
    data["se_groups"][0]["routing_profile_id"] = None
    with pytest.raises(RenderError, match="no routing profile"):
        render_keepalived(snapshot_from_mapping(data), GROUP, slot())


def test_profile_kind_must_match_ha_mode() -> None:
    from switchyard.domain.errors import DomainValidationError

    data = copy.deepcopy(load("01-vrrp"))
    data["se_groups"][0]["ha_mode"] = "bgp"
    with pytest.raises(DomainValidationError, match="profile_kind_mismatch"):
        render_keepalived(snapshot_from_mapping(data), GROUP, slot())


class TestUdp:
    """UDP services render to IPVS virtual servers in keepalived.conf (ADR 0020)."""

    def snapshot(self, case: str = "04-vrrp-udp") -> Any:
        return snapshot_from_mapping(load(case))

    def vrrp(self, data: dict[str, Any] | None = None) -> dict[str, str]:
        snap = snapshot_from_mapping(data) if data else self.snapshot()
        return render_keepalived(snap, GROUP, slot())

    def test_vrrp_group_holds_the_vrrp_instance_and_the_virtual_servers(self) -> None:
        conf = self.vrrp()["keepalived/keepalived.conf"]
        assert "vrrp_instance sy_edge" in conf
        assert "virtual_server 192.0.2.22 53 {" in conf
        assert "virtual_server 192.0.2.22 5353 {" in conf
        assert "virtual_server 192.0.2.23 51820 {" in conf
        assert "lb_kind NAT" in conf

    def test_unequal_weights_use_the_weighted_scheduler(self) -> None:
        conf = self.vrrp()["keepalived/keepalived.conf"]
        dns, wg = conf.split("# UDP service wireguard")
        assert "lb_algo wlc" in dns  # leastconn, weights 100/50/0
        assert "lb_algo sh" in wg  # source

    @pytest.mark.parametrize(
        ("algorithm", "plain", "weighted"),
        [("roundrobin", "rr", "wrr"), ("leastconn", "lc", "wlc"), ("source", "sh", "sh")],
    )
    def test_scheduler_mapping(self, algorithm: str, plain: str, weighted: str) -> None:
        data = load("04-vrrp-udp")
        pool = next(p for p in data["pools"] if p["name"] == "dns")
        pool["algorithm"] = algorithm
        assert f"lb_algo {weighted}" in self.vrrp(data)["keepalived/keepalived.conf"]
        for m in pool["members"]:
            m["weight"] = 100
            m["admin_state"] = "enabled"
        assert f"lb_algo {plain}\n" in self.vrrp(data)["keepalived/keepalived.conf"]

    def test_drain_has_weight_zero_and_disabled_members_are_omitted(self) -> None:
        conf = self.vrrp()["keepalived/keepalived.conf"]
        drained = conf.split("real_server 10.20.0.42 53 {")[1].split("UDP_CHECK")[0]
        assert "weight 0" in drained
        assert "10.20.0.43" not in conf

    def test_monitor_renders_a_udp_check(self) -> None:
        conf = self.vrrp()["keepalived/keepalived.conf"]
        assert "delay_loop 4" in conf
        assert "connect_port 5353" in conf
        assert "payload 0000010000010000000000000000010001" in conf
        assert "require_reply" in conf

    def test_a_pool_without_a_monitor_has_no_check(self) -> None:
        conf = self.vrrp()["keepalived/keepalived.conf"]
        wg = conf.split("# UDP service wireguard")[1]
        assert "UDP_CHECK" not in wg

    def test_host_files_ship_with_udp_services_only(self) -> None:
        files = self.vrrp()
        assert files.keys() == {
            "keepalived/keepalived.conf",
            "nftables/switchyard-udp.nft",
            "sysctl/switchyard-udp.conf",
        }
        assert "net.ipv4.vs.conntrack = 1" in files["sysctl/switchyard-udp.conf"]
        assert "10.20.0.50 . 51820" in files["nftables/switchyard-udp.nft"]
        assert "10.20.0.43" not in files["nftables/switchyard-udp.nft"]
        plain = render_keepalived(snapshot_from_mapping(load("01-vrrp")), GROUP, slot())
        assert plain.keys() == {"keepalived/keepalived.conf"}
        assert "virtual_server" not in plain["keepalived/keepalived.conf"]

    def test_bgp_group_gets_ipvs_only_keepalived_and_a_watchdog_list(self) -> None:
        snap = self.snapshot("05-bgp-udp")
        files = render_frr(snap, GROUP, slot(), {"kv/bgp/peer-1": "pw-1"})
        conf = files["keepalived/keepalived.conf"]
        assert "virtual_server 192.0.2.23 51820 {" in conf
        assert "vrrp_instance" not in conf
        assert "vrrp_script" not in conf
        vips = json.loads(files["frr/vips.json"])
        assert vips["udp_prefixes"] == ["192.0.2.22/32", "192.0.2.23/32"]
        assert vips["udp_only_prefixes"] == ["192.0.2.22/32", "192.0.2.23/32"]

    def test_bgp_group_without_udp_has_no_keepalived(self) -> None:
        files = render_frr(
            snapshot_from_mapping(load("02-bgp")), GROUP, slot(), {"kv/bgp/peer-1": "p"}
        )
        assert "keepalived/keepalived.conf" not in files

    def test_a_vip_shared_with_tcp_is_not_udp_only(self) -> None:
        data = load("05-bgp-udp")
        udp = next(v for v in data["virtual_services"] if v["name"] == "wireguard")
        api = next(v for v in data["virtual_services"] if v["name"] == "api")
        api["vip"] = udp["vip"]
        files = render_frr(snapshot_from_mapping(data), GROUP, slot(), {"kv/bgp/peer-1": "p"})
        assert "192.0.2.23/32" not in json.loads(files["frr/vips.json"])["udp_only_prefixes"]

    def test_disabled_udp_service_is_not_rendered(self) -> None:
        data = load("04-vrrp-udp")
        for v in data["virtual_services"]:
            if v["protocol"] == "udp":
                v["enabled"] = False
        files = self.vrrp(data)
        assert files.keys() == {"keepalived/keepalived.conf"}
        assert "virtual_server" not in files["keepalived/keepalived.conf"]

    def test_random_scheduler_is_refused(self) -> None:
        data = load("04-vrrp-udp")
        next(p for p in data["pools"] if p["name"] == "wireguard")["algorithm"] = "random"
        with pytest.raises(Exception, match="random"):
            self.vrrp(data)
