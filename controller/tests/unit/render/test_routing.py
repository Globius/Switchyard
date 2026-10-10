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

    def test_missing_secret_fails_instead_of_dropping_authentication(self) -> None:
        with pytest.raises(RenderError, match="missing secret kv/bgp/peer-1"):
            self.render(secrets={"other": "x"})

    @pytest.mark.parametrize("bad", ["pw 1", "pw\nrouter bgp 1", 'pw"', "pw!", "", "x" * 81])
    def test_unsafe_passwords_are_refused(self, bad: str) -> None:
        with pytest.raises(RenderError):
            self.render(secrets={"kv/bgp/peer-1": bad})

    def test_router_id_must_be_ipv4(self) -> None:
        with pytest.raises(RenderError, match="router_id"):
            self.render(router="not-an-ip")

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
