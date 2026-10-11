"""UDP field rules (ticket T7, ADR 0020): one test per rejected field, checking its path."""

import copy
from typing import Any

import pytest

from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.models import HealthMonitor, MonitorType
from switchyard.domain.snapshot import validate_snapshot
from tests.factories import BASE


def data_with_udp(**service: Any) -> dict[str, Any]:
    data = copy.deepcopy(BASE)
    data["pools"].append(
        {
            "id": "pool-wg",
            "tenant_id": "t-acme",
            "name": "wg",
            "members": [{"address": "10.20.0.50", "port": 51820}],
        }
    )
    vs = {
        "id": "vs-wg",
        "tenant_id": "t-acme",
        "se_group_id": "seg-1",
        "name": "wg",
        "protocol": "udp",
        "vip": "192.0.2.40",
        "ports": [{"port": 51820}],
        "default_pool_id": "pool-wg",
    }
    data["virtual_services"].append(vs | service)
    return data


def problems(data: dict[str, Any]) -> list[tuple[str, str]]:
    return [(p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(data))]


def test_a_plain_udp_service_is_valid() -> None:
    assert problems(data_with_udp()) == []


@pytest.mark.parametrize(
    ("extra", "path"),
    [
        ({"certificate_ids": ["cert-wild"]}, "certificate_ids"),
        ({"allow_list_id": "ps-x"}, "allow_list_id"),
        ({"policy_set_ids": ["ps-x"]}, "policy_set_ids"),
        ({"routes": [{"match": {"host": "a.example.test"}, "pool_id": "pool-wg"}]}, "routes"),
        ({"restore_client_ip_header": "CF-Connecting-IP"}, "restore_client_ip_header"),
        ({"ports": [{"port": 51820, "tls": True}]}, "ports[0].tls"),
        ({"ports": [{"port": 51820, "proxy_protocol": True}]}, "ports[0].proxy_protocol"),
        ({"ports": [{"port": 51820, "tls": True, "http2": True}]}, "ports[0].http2"),
        ({"ports": [{"port": 51820, "https_redirect_port": 443}]}, "ports[0].https_redirect_port"),
    ],
)
def test_each_forbidden_service_field_reports_its_path(extra: dict[str, Any], path: str) -> None:
    found = [p for p in problems(data_with_udp(**extra)) if p[1] == "udp_field_not_allowed"]
    assert f"virtual_services.wg.{path}" in [p[0] for p in found]


def test_enforce_allow_list_is_reported_on_its_field() -> None:
    found = problems(data_with_udp(enforce_allow_list=True))
    assert ("virtual_services.wg.enforce_allow_list", "udp_field_not_allowed") in found


def pool(data: dict[str, Any]) -> dict[str, Any]:
    return next(p for p in data["pools"] if p["name"] == "wg")


def test_pool_tls_persistence_and_random_are_rejected() -> None:
    data = data_with_udp()
    pool(data).update({"tls": True, "persistence": {"type": "source_ip"}, "algorithm": "random"})
    assert problems(data) == [
        ("pools.wg.algorithm", "udp_algorithm_unsupported"),
        ("pools.wg.persistence", "udp_field_not_allowed"),
        ("pools.wg.tls", "udp_field_not_allowed"),
    ]


def test_member_without_port_is_rejected() -> None:
    data = data_with_udp()
    pool(data)["members"] = [{"address": "10.20.0.50"}]
    assert problems(data) == [("pools.wg.members.10-20-0-50.port", "udp_member_port_required")]


def monitor(kind: str, **extra: Any) -> dict[str, Any]:
    return {"id": "hm-1", "tenant_id": "t-acme", "name": "m", "type": kind} | extra


@pytest.mark.parametrize("kind", ["tcp", "http"])
def test_tcp_and_http_monitors_cannot_check_a_udp_pool(kind: str) -> None:
    data = data_with_udp()
    extra = {"http": {"path": "/"}} if kind == "http" else {}
    data["health_monitors"] = [monitor(kind, **extra)]
    pool(data)["health_monitor_id"] = "hm-1"
    assert problems(data) == [("pools.wg.health_monitor_id", "monitor_protocol_mismatch")]


def test_udp_monitor_cannot_check_a_tcp_pool() -> None:
    data = copy.deepcopy(BASE)
    data["health_monitors"] = [monitor("udp")]
    data["pools"][0]["health_monitor_id"] = "hm-1"
    assert problems(data) == [("pools.web.health_monitor_id", "monitor_protocol_mismatch")]


def test_udp_monitor_on_a_udp_pool_is_valid() -> None:
    data = data_with_udp()
    data["health_monitors"] = [monitor("udp", udp={"payload_hex": "00ff", "require_reply": True})]
    pool(data)["health_monitor_id"] = "hm-1"
    assert problems(data) == []


def test_pool_shared_by_tcp_and_udp_is_rejected() -> None:
    data = data_with_udp()
    data["virtual_services"].append(
        {
            "id": "vs-tcp",
            "tenant_id": "t-acme",
            "se_group_id": "seg-1",
            "name": "tcp",
            "protocol": "tcp",
            "vip": "192.0.2.41",
            "ports": [{"port": 6443}],
            "default_pool_id": "pool-wg",
        }
    )
    assert ("pools.wg", "pool_protocol_mismatch") in problems(data)


class TestMonitorModel:
    def test_udp_monitor_has_no_http_block(self) -> None:
        with pytest.raises(ValueError, match="http block"):
            HealthMonitor.model_validate(monitor("udp", http={"path": "/"}) | _ids())

    def test_udp_block_is_only_for_udp(self) -> None:
        with pytest.raises(ValueError, match="udp block"):
            HealthMonitor.model_validate(monitor("tcp", udp={}) | _ids())

    def test_payload_must_be_even_length_lowercase_hex(self) -> None:
        for bad in ("0", "zz", "0A"):
            with pytest.raises(ValueError, match="payload_hex"):
                HealthMonitor.model_validate(monitor("udp", udp={"payload_hex": bad}) | _ids())
        ok = HealthMonitor.model_validate(monitor("udp", udp={"payload_hex": "00ff"}) | _ids())
        assert ok.type is MonitorType.UDP


def _ids() -> dict[str, Any]:
    import uuid

    return {"id": uuid.UUID(int=1), "tenant_id": uuid.UUID(int=2)}


@pytest.mark.parametrize("field", ["passive_check", "slow_start_s"])
def test_passive_checks_and_slow_start_are_not_valid_on_a_udp_pool(field: str) -> None:
    data = data_with_udp()
    value = {"observe": "layer4"} if field == "passive_check" else 30
    data["pools"][-1][field] = value
    assert (f"pools.wg.{field}", "udp_field_not_allowed") in problems(data)
