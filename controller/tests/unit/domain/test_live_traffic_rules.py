"""Passive checks and slow start (ADR 0066): what pools may use them."""

import copy
from typing import Any

from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import validate_snapshot
from tests.factories import BASE

MONITOR = {
    "id": "hm-tcp",
    "tenant_id": "t-acme",
    "name": "tcp",
    "type": "tcp",
    "interval_s": 5,
    "timeout_s": 2,
    "rise": 2,
    "fall": 3,
}


def data(
    pool: dict[str, Any], *, monitor: bool = True, tcp_service: bool = False
) -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    if monitor:
        d["health_monitors"] = [MONITOR]
        d["pools"][0]["health_monitor_id"] = "hm-tcp"
    d["pools"][0].update(pool)
    if tcp_service:
        d["virtual_services"].append(
            {
                "id": "vs-tcp",
                "tenant_id": "t-acme",
                "se_group_id": "seg-1",
                "name": "raw",
                "protocol": "tcp",
                "vip": "192.0.2.41",
                "ports": [{"port": 9000}],
                "default_pool_id": "pool-web",
            }
        )
        d["virtual_services"][0]["routes"] = []
        d["virtual_services"][0]["default_pool_id"] = "pool-web"
    return d


def problems(d: dict[str, Any]) -> list[tuple[str, str]]:
    return [(p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(d))]


def test_both_are_fine_with_a_health_monitor() -> None:
    pool = {"passive_check": {"observe": "layer7", "error_limit": 3}, "slow_start_s": 30}
    assert problems(data(pool)) == []


def test_both_need_a_health_monitor() -> None:
    pool = {"passive_check": {}, "slow_start_s": 30}
    assert problems(data(pool, monitor=False)) == [
        ("pools.web.passive_check", "needs_health_monitor"),
        ("pools.web.slow_start_s", "needs_health_monitor"),
    ]


def test_layer7_observation_is_refused_on_a_tcp_pool_but_layer4_is_fine() -> None:
    base = data({"passive_check": {"observe": "layer7"}}, tcp_service=True)
    got = problems(base)
    assert ("pools.web.passive_check.observe", "passive_check_layer7_needs_http") in got
    assert ("pools.web", "pool_protocol_mismatch") in got  # shared with the http service
    only_tcp = copy.deepcopy(base)
    only_tcp["virtual_services"] = [only_tcp["virtual_services"][1]]
    assert problems(only_tcp) == [
        ("pools.web.passive_check.observe", "passive_check_layer7_needs_http")
    ]
    only_tcp["pools"][0]["passive_check"] = {"observe": "layer4"}
    assert problems(only_tcp) == []


def test_bounds_are_validated_by_the_model() -> None:
    import pytest
    from pydantic import ValidationError

    from switchyard.domain.models import PassiveCheck

    for bad in ({"error_limit": 0}, {"error_limit": 1001}, {"on_error": "explode"}):
        with pytest.raises(ValidationError):
            PassiveCheck(**bad)
