"""IPVS direct-return and tunnel rules (ADR 0192): port, adjacency and protocol checks."""

import copy
from typing import Any

import pytest

from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import validate_snapshot
from tests.factories import BASE


def build(
    mode: str, member: str = "10.20.0.50", port: int = 5004, **service: Any
) -> dict[str, Any]:
    data = copy.deepcopy(BASE)
    data["pools"].append(
        {
            "id": "pool-m",
            "tenant_id": "t-acme",
            "name": "media",
            "members": [{"address": member, "port": port}],
        }
    )
    data["virtual_services"].append(
        {
            "id": "vs-m",
            "tenant_id": "t-acme",
            "se_group_id": "seg-1",
            "name": "media",
            "protocol": "udp",
            "forward_mode": mode,
            "vip": "192.0.2.40",
            "ports": [{"port": 5004}],
            "default_pool_id": "pool-m",
        }
        | service
    )
    return data


def codes(data: dict[str, Any]) -> list[tuple[str, str]]:
    return [(p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(data))]


def test_nat_needs_no_extra_rules() -> None:
    assert codes(build("nat", member="10.20.0.50", port=9999)) == []


def test_direct_return_on_the_backend_segment_is_valid() -> None:
    assert codes(build("dr")) == []


def test_tunnel_may_reach_a_remote_member() -> None:
    assert codes(build("tunnel", member="10.99.4.10")) == []


def test_direct_return_rejects_a_member_outside_the_backend_networks() -> None:
    found = codes(build("dr", member="10.99.4.10"))
    assert [c for _, c in found] == ["ipvs_dr_not_adjacent"]


def test_dr_and_tunnel_cannot_remap_the_port() -> None:
    for mode in ("dr", "tunnel"):
        found = codes(build(mode, port=8080))
        assert any(c == "ipvs_port_mismatch" for _, c in found), mode


def test_forward_mode_is_only_for_udp() -> None:
    data = build("dr")
    data["virtual_services"][-1]["protocol"] = "tcp"
    assert any(c == "forward_mode_not_allowed" for _, c in codes(data))


def test_dns_publication_rules() -> None:
    from pydantic import ValidationError

    from switchyard.domain.models import DnsPublication

    assert DnsPublication(names=("App.Example.test",)).ttl == 300
    for bad in (("*.example.test",), ("a.example.test", "A.example.test"), ("not a host",)):
        with pytest.raises(ValidationError):
            DnsPublication(names=bad)


def test_a_udp_service_cannot_be_proxied() -> None:
    data = build("nat", dns={"names": ["m.example.test"], "proxied": True})
    assert ("virtual_services.media.dns.proxied", "dns_proxy_not_allowed") in codes(data)


def test_a_name_belongs_to_one_service() -> None:
    data = build("nat", dns={"names": ["app.example.test"]})
    data["virtual_services"][0]["dns"] = {"names": ["app.example.test"]}
    assert any(c == "dns_name_taken" for _, c in codes(data))
