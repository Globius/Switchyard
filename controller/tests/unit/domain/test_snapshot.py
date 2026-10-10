"""Tests for cross-object snapshot validation and the fixture loader."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest

from switchyard.domain.errors import DomainValidationError
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.domain.snapshot import Snapshot, ensure_valid, validate_snapshot
from tests.factories import base_data, base_snapshot


def codes(data: dict[str, Any]) -> list[str]:
    return [p.code for p in validate_snapshot(snapshot_from_mapping(data))]


def test_base_snapshot_is_valid() -> None:
    assert validate_snapshot(base_snapshot()) == []
    ensure_valid(base_snapshot())


def test_ensure_valid_raises_with_all_problems() -> None:
    data = base_data()
    data["virtual_services"][0]["vip"] = "198.51.100.5"
    with pytest.raises(DomainValidationError) as exc:
        ensure_valid(snapshot_from_mapping(data))
    assert [p.code for p in exc.value.problems] == ["vip_outside_network", "vip_outside_pool"]
    assert "198.51.100.5" in str(exc.value)


Mutation = Callable[[dict[str, Any]], None]


def _set(path: list[Any], value: Any) -> Mutation:
    def apply(data: dict[str, Any]) -> None:
        node = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return apply


def _add_vs(**kw: Any) -> Mutation:
    def apply(data: dict[str, Any]) -> None:
        vs = {**data["virtual_services"][0], "id": "vs-2", "name": "second", **kw}
        data["virtual_services"].append(vs)

    return apply


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        pytest.param(
            _set(["virtual_services", 0, "vip"], "192.0.2.255"),
            ["vip_outside_network", "vip_outside_pool"],
            id="broadcast",
        ),
        pytest.param(
            _set(["virtual_services", 0, "vip"], "10.20.0.50"),
            ["vip_outside_network", "vip_outside_pool"],
            id="backend-net",
        ),
        pytest.param(
            _set(["virtual_services", 0, "vip"], "192.0.2.5"), ["vip_outside_pool"], id="below-pool"
        ),
        pytest.param(
            _set(["virtual_services", 0, "se_group_id"], "nope"),
            ["unknown_reference"],
            id="no-group",
        ),
        pytest.param(
            _set(["virtual_services", 0, "certificate_ids"], ["nope"]),
            ["unknown_reference", "certificate_coverage"],
            id="no-cert",
        ),
        pytest.param(
            _set(["virtual_services", 0, "default_pool_id"], "nope"),
            ["unknown_reference"],
            id="no-pool",
        ),
        pytest.param(
            _set(["certificates", 0, "sans"], ["*.other.test"]),
            ["certificate_coverage"],
            id="san-mismatch",
        ),
        pytest.param(
            _set(["certificates", 0, "sans"], ["example.test"]),
            ["certificate_coverage"],
            id="wildcard-missing",
        ),
        pytest.param(
            _set(["virtual_services", 0, "routes", 0, "match", "host"], "a.b.example.test"),
            ["certificate_coverage"],
            id="two-labels-deep",
        ),
        pytest.param(_add_vs(), ["port_collision", "port_collision"], id="collision"),
        pytest.param(_add_vs(vip="192.0.2.21", vip_pool_id="vp-main"), [], id="second-vip-ok"),
        pytest.param(_add_vs(vip="192.0.2.21", name="web"), ["duplicate_name"], id="dup-name"),
        pytest.param(
            _set(
                ["virtual_services", 0, "routes"],
                [
                    {"match": {"host": "a.example.test"}, "pool_id": "pool-web"},
                    {"match": {"host": "a.example.test"}, "pool_id": "pool-web"},
                ],
            ),
            ["duplicate_route"],
            id="dup-route",
        ),
    ],
)
def test_rules(mutate: Mutation, expected: list[str]) -> None:
    data = base_data()
    mutate(data)
    assert sorted(codes(data)) == sorted(expected)


def test_unknown_tenant_is_reported_once_per_object() -> None:
    data = base_data()
    data["tenants"] = []
    result = validate_snapshot(snapshot_from_mapping(data))
    assert {p.code for p in result} == {"unknown_reference"}
    assert len(result) == 7  # network x2, vip pool, SE group, VS, pool, certificate


def test_cross_tenant_reference_rejected() -> None:
    data = base_data()
    data["tenants"].append({"id": "t-other", "name": "other"})
    data["pools"][0]["tenant_id"] = "t-other"
    assert "wrong_tenant" in codes(data)


def test_same_name_in_different_tenants_is_fine() -> None:
    data = base_data()
    data["tenants"].append({"id": "t-other", "name": "other"})
    data["networks"].append(
        {"id": "net-x", "tenant_id": "t-other", "name": "lb", "cidr": "198.51.100.0/24"}
    )
    assert codes(data) == []


def test_pool_shared_between_http_and_tcp_rejected() -> None:
    data = base_data()
    data["virtual_services"].append(
        {
            "id": "vs-tcp",
            "tenant_id": "t-acme",
            "se_group_id": "seg-1",
            "name": "tcp",
            "protocol": "tcp",
            "vip": "192.0.2.22",
            "ports": [{"port": 6443}],
            "default_pool_id": "pool-web",
        }
    )
    assert codes(data) == ["pool_protocol_mismatch"]


def test_tcp_and_udp_same_port_not_a_collision() -> None:
    data = base_data()
    for proto, i in (("tcp", 1), ("udp", 2)):
        data["virtual_services"].append(
            {
                "id": f"vs-{proto}",
                "tenant_id": "t-acme",
                "se_group_id": "seg-1",
                "name": f"dns-{proto}",
                "protocol": proto,
                "vip": "192.0.2.30",
                "ports": [{"port": 53}],
                "default_pool_id": f"pool-dns{i}",
            }
        )
        data["pools"].append({"id": f"pool-dns{i}", "tenant_id": "t-acme", "name": f"dns{i}"})
    assert codes(data) == []


def test_monitored_member_without_port_needs_a_monitor_port() -> None:
    data = base_data()
    data["health_monitors"] = [{"id": "hm", "tenant_id": "t-acme", "name": "tcp", "type": "tcp"}]
    data["pools"][0].update(health_monitor_id="hm", members=[{"address": "10.20.0.11"}])
    assert codes(data) == ["check_port_unknown"]
    data["health_monitors"][0]["port"] = 8080
    assert codes(data) == []


def test_wrong_policy_kind() -> None:
    data = base_data()
    data["policy_sets"] = [
        {
            "id": "ps-cf",
            "tenant_id": "t-acme",
            "name": "cf",
            "kind": "allow_list",
            "cidrs": ["192.0.2.0/24"],
        }
    ]
    data["virtual_services"][0]["policy_set_ids"] = ["ps-cf"]
    assert codes(data) == ["wrong_policy_kind"]


def test_wildcard_route_host_requires_wildcard_san() -> None:
    data = base_data()
    data["virtual_services"][0]["routes"][0]["match"]["host"] = "*.example.test"
    assert codes(data) == []
    data["certificates"][0]["sans"] = ["example.test", "app.example.test"]
    assert codes(data) == ["certificate_coverage"]


def test_vip_pool_range_outside_network() -> None:
    data = base_data()
    data["vip_pools"][0]["ranges"] = [{"start": "192.0.2.200", "end": "192.0.3.5"}]
    assert "vip_outside_network" in codes(data)


def test_for_se_group_filters_and_validates() -> None:
    data = base_data()
    data["se_groups"].append(
        {
            "id": "seg-2",
            "tenant_id": "t-acme",
            "name": "other",
            "ha_mode": "bgp",
            "networks": [{"network_id": "net-lb", "role": "vip"}],
        }
    )
    data["pools"].append({"id": "pool-unused", "tenant_id": "t-acme", "name": "unused"})
    snap = snapshot_from_mapping(data)
    sub = snap.for_se_group(uuid.UUID(symbolic_uuid("seg-1")))
    assert [g.name for g in sub.se_groups] == ["edge"]
    assert [p.name for p in sub.pools] == ["web"]
    assert validate_snapshot(sub) == []
    with pytest.raises(KeyError):
        snap.for_se_group(uuid.UUID(int=5))


def test_problems_are_sorted_and_deterministic() -> None:
    data = base_data()
    data["virtual_services"][0]["vip"] = "198.51.100.5"
    data["certificates"][0]["sans"] = ["nothing.test"]
    first = validate_snapshot(snapshot_from_mapping(data))
    second = validate_snapshot(snapshot_from_mapping(data))
    assert first == second == sorted(first)


class TestLoader:
    def test_symbolic_ids_are_stable_and_distinct(self) -> None:
        assert symbolic_uuid("a") == symbolic_uuid("a")
        assert symbolic_uuid("a") != symbolic_uuid("b")

    def test_real_uuid_passes_through(self) -> None:
        u = "01890a5d-ac96-774b-bcce-b302099a8057"
        assert symbolic_uuid(u) == u

    def test_member_ids_default_deterministically(self) -> None:
        a = base_snapshot().pools[0].members[0].id
        b = base_snapshot().pools[0].members[0].id
        assert a == b

    def test_empty_snapshot(self) -> None:
        assert snapshot_from_mapping({}) == Snapshot()
