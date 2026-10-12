"""Pool persistence, member options, backend mTLS and health check presets (ADRs 0105 to 0108)."""

import copy
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain import models as dm
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import validate_snapshot
from switchyard.pipeline.bundles import referenced_certs
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import render_haproxy
from tests.factories import BASE

TCP_SERVICE = {
    "id": "vs-tcp",
    "tenant_id": "t-acme",
    "se_group_id": "seg-1",
    "name": "raw",
    "protocol": "tcp",
    "vip": "192.0.2.41",
    "ports": [{"port": 9000}],
    "default_pool_id": "pool-web",
}
UDP_SERVICE = {**TCP_SERVICE, "name": "dns", "protocol": "udp", "ports": [{"port": 53}]}
CLIENT_CA = {
    "id": "cert-ca",
    "tenant_id": "t-acme",
    "name": "client-ca",
    "source": "upload",
    "purpose": "client_ca",
    "sans": [],
    "not_after": "2038-01-01T00:00:00+00:00",
    "secret_ref": "kv/tenants/acme/certs/client-ca",
}
BACKEND_CERT = {
    "id": "cert-backend",
    "tenant_id": "t-acme",
    "name": "backend-client",
    "source": "upload",
    "sans": ["se.internal.test"],
    "not_after": "2038-01-01T00:00:00+00:00",
    "secret_ref": "kv/tenants/acme/certs/backend-client",
}


def pool_payload(**extra: Any) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "tenant_id": str(uuid.uuid4()), "name": "p", **extra}


def data(pool: dict[str, Any], service: dict[str, Any] | None = None) -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    d["pools"][0].update(pool)
    if service is not None:
        d["virtual_services"] = [{**service, "default_pool_id": "pool-web"}]
    return d


def problems(d: dict[str, Any]) -> list[tuple[str, str]]:
    return [(p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(d))]


# -- A1: persistence ---------------------------------------------------------------------------


def test_every_persistence_kind_loads_from_its_type() -> None:
    kinds = {
        "source_ip": {},
        "http_cookie": {"name": "SY"},
        "header": {"header": "X-Session"},
        "uri": {"source": "path"},
    }
    for kind, extra in kinds.items():
        pool = dm.Pool.model_validate(pool_payload(persistence={"type": kind, **extra}))
        assert pool.persistence is not None
        assert pool.persistence.type == kind


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "http_cookie", "name": "a;b"},
        {"type": "http_cookie", "name": "SY", "mode": "prefix", "ttl_s": 60},
        {"type": "http_cookie", "name": "SY", "mode": "rewrite", "secure": True},
        {"type": "header", "header": "X Bad"},
        {"type": "uri", "source": "url_param"},
        {"type": "uri", "source": "path", "param": "x"},
        {"type": "uri", "source": "url_param", "param": "Bad Name"},
        {"type": "nonsense"},
    ],
)
def test_bad_persistence_is_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        dm.Pool.model_validate(pool_payload(persistence=bad))


def test_cookie_header_and_uri_persistence_need_http() -> None:
    for persistence in (
        {"type": "http_cookie", "name": "SY"},
        {"type": "header", "header": "X-Session"},
        {"type": "uri", "source": "path"},
    ):
        d = data({"persistence": persistence}, TCP_SERVICE)
        assert problems(d) == [("pools.web.persistence", "persistence_needs_http")]
        assert problems(data({"persistence": persistence})) == []
    assert problems(data({"persistence": {"type": "source_ip"}}, TCP_SERVICE)) == []


def test_persistence_is_refused_on_udp_pools() -> None:
    d = data({"persistence": {"type": "header", "header": "X-Session"}}, UDP_SERVICE)
    assert ("pools.web.persistence", "udp_field_not_allowed") in problems(d)


# -- A6: member and pool options ---------------------------------------------------------------


def test_member_options_are_bounded() -> None:
    base: dict[str, Any] = {"id": uuid.uuid4(), "address": "10.0.0.5"}
    assert dm.PoolMember(**base, backup=True, maxconn=10).maxconn == 10
    for bad in (0, 1_000_001):
        with pytest.raises(ValidationError):
            dm.PoolMember(**base, maxconn=bad)


def test_udp_pools_refuse_proxy_protocol_and_member_options() -> None:
    d = data({"send_proxy": "v2"}, UDP_SERVICE)
    d["pools"][0]["members"] = [
        {"address": "10.20.0.5", "port": 53, "backup": True, "maxconn": 5},
    ]
    assert problems(d) == [
        ("pools.web.members.10-20-0-5-53.backup", "udp_field_not_allowed"),
        ("pools.web.members.10-20-0-5-53.maxconn", "udp_field_not_allowed"),
        ("pools.web.send_proxy", "udp_field_not_allowed"),
    ]


def test_send_proxy_only_takes_v1_or_v2() -> None:
    with pytest.raises(ValidationError):
        dm.Pool.model_validate(pool_payload(send_proxy="v3"))


# -- A7: backend mTLS --------------------------------------------------------------------------


def mtls() -> dict[str, Any]:
    d = data({"tls": True, "client_certificate_id": "cert-backend"})
    d["certificates"].append(BACKEND_CERT)
    return d


def test_client_certificate_needs_tls() -> None:
    with pytest.raises(ValidationError, match="client_certificate_id requires tls"):
        dm.Pool.model_validate(pool_payload(client_certificate_id=str(uuid.uuid4())))


def test_client_certificate_must_exist_and_have_a_key() -> None:
    assert problems(mtls()) == []
    d = mtls()
    d["pools"][0]["client_certificate_id"] = "cert-missing"
    assert problems(d) == [("pools.web.client_certificate_id", "unknown_reference")]
    d = mtls()
    d["certificates"].append(CLIENT_CA)
    d["pools"][0]["client_certificate_id"] = "cert-ca"
    assert problems(d) == [("pools.web.client_certificate_id", "wrong_certificate_purpose")]


def test_the_certificate_reaches_the_group_view_the_bundle_and_the_server_line() -> None:
    snap = snapshot_from_mapping(mtls())
    group = snap.se_groups[0].id
    assert [c.name for c in referenced_certs(snap, group)] == [
        "backend-client",
        "wild-example-test",
    ]
    assert sorted(c.name for c in snap.for_se_group(group).certificates) == [
        "backend-client",
        "wild-example-test",
    ]
    cfg = render_haproxy(snap, group).files["haproxy/haproxy.cfg"]
    assert "ssl verify none crt /etc/switchyard/certs/backend-client.pem" in cfg


# -- A8: health check presets ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "ok"),
    [
        ({"type": "tcp", "preset": "redis"}, True),
        ({"type": "tcp", "preset": "postgres"}, False),
        ({"type": "udp", "preset": "redis"}, False),
        ({"type": "http", "http": {}, "preset": "smtp"}, False),
        ({"type": "http", "http": {"expect_body": "ok_1"}}, True),
        ({"type": "http", "http": {"expect_body": "has space"}}, False),
        ({"type": "http", "http": {"expect_body": 'q"uote'}}, False),
        ({"type": "http", "http": {"expect_header": {"name": "X-Health", "value": "ok"}}}, True),
        ({"type": "http", "http": {"expect_header": {"name": "X Bad"}}}, False),
        ({"type": "http", "http": {"expect_status": [200], "expect_body": "ok"}}, False),
        ({"type": "http", "http": {"expect_body": "ok", "expect_header": {"name": "X"}}}, False),
    ],
)
def test_monitor_presets_and_expectations(kwargs: dict[str, Any], ok: bool) -> None:
    payload = pool_payload(**kwargs)
    if ok:
        dm.HealthMonitor.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            dm.HealthMonitor.model_validate(payload)


def test_a_tcp_preset_is_refused_on_a_udp_pool() -> None:
    d = data({"health_monitor_id": "hm-redis"}, UDP_SERVICE)
    d["pools"][0]["members"] = [{"address": "10.20.0.5", "port": 53}]
    d["health_monitors"] = [
        {"id": "hm-redis", "tenant_id": "t-acme", "name": "redis", "type": "tcp", "preset": "redis"}
    ]
    assert ("pools.web.health_monitor_id", "monitor_protocol_mismatch") in problems(d)


# -- renderer guards ---------------------------------------------------------------------------


def test_the_renderer_refuses_an_unsafe_cookie_name_even_if_validation_was_bypassed() -> None:
    snap = snapshot_from_mapping(data({"persistence": {"type": "http_cookie", "name": "SY"}}))
    bad = dm.PersistenceCookie.model_construct(type="http_cookie", name="a b", mode="insert")
    pool = snap.pools[0].model_copy(update={"persistence": bad})
    broken = snap.model_copy(update={"pools": (pool,)})
    with pytest.raises(RenderError, match="cookie name"):
        render_haproxy(broken, broken.se_groups[0].id)
