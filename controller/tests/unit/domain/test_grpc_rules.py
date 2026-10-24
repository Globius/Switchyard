# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The ``grpc`` monitor kind and its ``h2``/``h2c`` requirement (ADR 0227)."""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain import models as dm
from switchyard.domain.field_rules import protocol_rules
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import validate_snapshot
from tests.factories import BASE

MONITOR = {
    "id": "hm-grpc",
    "tenant_id": "t-acme",
    "name": "grpc-health",
    "type": "grpc",
    "grpc": {"service": "acme.echo.Health", "expect_status": [200]},
}


def data(pool: dict[str, Any], monitor: dict[str, Any] | None = None) -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    d["health_monitors"] = [dict(MONITOR if monitor is None else monitor)]
    d["pools"][0].update(health_monitor_id="hm-grpc", **pool)
    return d


def problems(d: dict[str, Any]) -> list[tuple[str, str]]:
    return [(p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(d))]


def monitor(**kwargs: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tenant_id": "00000000-0000-0000-0000-000000000002",
        "id": "00000000-0000-0000-0000-000000000001",
        "name": "grpc-health",
        "type": "grpc",
        "grpc": {"service": "acme.echo.Health"},
    }
    payload.update(kwargs)
    return payload


def test_a_grpc_monitor_needs_an_h2_pool() -> None:
    assert problems(data({})) == [("pools.web.health_monitor_id", "grpc_check_needs_h2")]


def test_h2c_and_h2_pools_accept_a_grpc_monitor() -> None:
    assert problems(data({"backend_protocol": "h2c"})) == []
    assert problems(data({"backend_protocol": "h2", "tls": True})) == []


def test_a_grpc_monitor_on_a_udp_pool_is_a_protocol_mismatch() -> None:
    d = copy.deepcopy(BASE)
    d["health_monitors"] = [dict(MONITOR)]
    d["pools"][0].update(health_monitor_id="hm-grpc")
    d["virtual_services"] = [
        {
            "id": "vs-udp",
            "tenant_id": "t-acme",
            "se_group_id": "seg-1",
            "name": "dns",
            "protocol": "udp",
            "vip": "192.0.2.42",
            "ports": [{"port": 53}],
            "default_pool_id": "pool-web",
        }
    ]
    d["virtual_services"][0]["default_pool_id"] = "pool-web"
    got = problems(d)
    assert ("pools.web.health_monitor_id", "monitor_protocol_mismatch") in got
    assert ("pools.web.health_monitor_id", "grpc_check_needs_h2") in got


def test_the_grpc_block_is_required_and_exclusive() -> None:
    assert dm.HealthMonitor.model_validate(monitor()).grpc is not None
    with pytest.raises(ValidationError, match="needs a grpc block"):
        dm.HealthMonitor.model_validate(monitor(grpc=None))
    with pytest.raises(ValidationError, match="cannot have a grpc block"):
        dm.HealthMonitor.model_validate(monitor(type="tcp"))
    with pytest.raises(ValidationError, match="cannot have an http block"):
        dm.HealthMonitor.model_validate({**monitor(), "http": {"path": "/healthz"}})


@pytest.mark.parametrize(
    "service",
    ["acme.echo.Health", "health", "a_b-c.d", ""],
)
def test_accepted_service_names(service: str) -> None:
    assert dm.GrpcCheck(service=service).service == service


@pytest.mark.parametrize("service", ["a b", "a/b", "a:b", "a" * 129, "café"])
def test_refused_service_names(service: str) -> None:
    with pytest.raises(ValidationError):
        dm.GrpcCheck(service=service)


def test_expect_status_is_bounded() -> None:
    assert dm.GrpcCheck(expect_status=(200, 204)).expect_status == (200, 204)
    with pytest.raises(ValidationError, match="at least one"):
        dm.GrpcCheck(expect_status=())
    with pytest.raises(ValidationError, match="between 100 and 599"):
        dm.GrpcCheck(expect_status=(99,))


def test_interval_bounds_are_the_monitor_ones() -> None:
    assert dm.HealthMonitor.model_validate(monitor(interval_s=1)).interval_s == 1
    with pytest.raises(ValidationError):
        dm.HealthMonitor.model_validate(monitor(interval_s=0))
    with pytest.raises(ValidationError):
        dm.HealthMonitor.model_validate(monitor(interval_s=301))


def test_http_services_may_use_a_grpc_monitor_and_tcp_and_udp_services_may_not() -> None:
    available = protocol_rules(dm.Protocol.HTTP).monitor_types
    assert dm.MonitorType.GRPC in available
    assert dm.MonitorType.GRPC not in protocol_rules(dm.Protocol.TCP).monitor_types
    assert protocol_rules(dm.Protocol.UDP).monitor_types == (dm.MonitorType.UDP,)
