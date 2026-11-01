# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Per-route client-IP restoration override: validation and precedence (ADR 0230)."""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain.models import RouteClientIp, VirtualService

T = uuid.UUID(int=1)
L = uuid.UUID(int=2)
P = uuid.UUID(int=12)


def route(host: str | None = "a.example.test", **extra: Any) -> dict[str, Any]:
    match: dict[str, Any] = {"host": host, **extra.pop("match", {})}
    return {"match": match, "pool_id": P, **extra}


def vs(routes: list[dict[str, Any]], **over: Any) -> VirtualService:
    data: dict[str, Any] = {
        "id": uuid.UUID(int=10),
        "tenant_id": T,
        "name": "svc",
        "se_group_id": uuid.UUID(int=11),
        "protocol": "http",
        "vip": "192.0.2.20",
        "ports": [{"port": 80}],
        "default_pool_id": P,
        "routes": routes,
    }
    data.update(over)
    return VirtualService.model_validate(data)


def test_absent_means_inherit() -> None:
    assert vs([route()]).routes[0].client_ip is None


def test_disabled_takes_nothing_else() -> None:
    assert RouteClientIp(enabled=False).header is None
    with pytest.raises(ValidationError, match="takes no header"):
        RouteClientIp(enabled=False, header="X-Real-Client")


def test_header_needs_a_list_or_explicit_any_source() -> None:
    with pytest.raises(ValidationError, match="needs header"):
        RouteClientIp()
    with pytest.raises(ValidationError, match=r"allow_list_id.*from_any_source=true"):
        RouteClientIp(header="X-Real-Client")
    with pytest.raises(ValidationError, match="conflicts"):
        RouteClientIp(header="X-Real-Client", allow_list_id=L, from_any_source=True)
    assert RouteClientIp(header="X-Real-Client", allow_list_id=L).allow_list_id == L


def test_only_on_host_only_routes() -> None:
    ci = {"enabled": False}
    assert vs([route(client_ip=ci)]).routes[0].client_ip is not None
    assert vs([route("*.example.test", client_ip=ci)])
    with pytest.raises(ValidationError, match="host-only"):
        vs([route(match={"path_prefix": "/api"}, client_ip=ci)])
    with pytest.raises(ValidationError, match="host-only"):
        vs([route(None, match={"path_prefix": "/api"}, client_ip=ci)])
    with pytest.raises(ValidationError, match="host-only"):
        vs([route(match={"methods": ["GET"]}, client_ip=ci)])


def test_one_override_per_host_and_http_only() -> None:
    ci = {"enabled": False}
    with pytest.raises(ValidationError, match="at most one route per host"):
        vs([route(client_ip=ci), route(client_ip=ci)])
    with pytest.raises(ValidationError):
        vs([route(client_ip=ci)], protocol="tcp", ports=[{"port": 443}])


def test_service_level_rules_unchanged_by_the_new_field() -> None:
    with pytest.raises(ValidationError, match="restore_client_ip_from_any_source=true"):
        vs([route()], restore_client_ip_header="CF-Connecting-IP")
