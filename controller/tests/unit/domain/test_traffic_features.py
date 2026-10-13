# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""SNI passthrough, HTTP/3, compression, backend HTTP/2 and tunnel timeout (ADRs 0115 to 0117)."""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain import models as dm
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.snapshot import validate_snapshot
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import render_haproxy
from tests.factories import BASE
from tests.unit.domain.test_pool_health_parity import TCP_SERVICE, UDP_SERVICE, data, pool_payload

PASSTHROUGH = {
    **TCP_SERVICE,
    "ports": [{"port": 443, "tls_passthrough": True}],
    "routes": [{"match": {"host": "mail.example.test"}, "pool_id": "pool-web"}],
}


def problems(d: dict[str, Any]) -> list[tuple[str, str]]:
    return [(p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(d))]


def service(**extra: Any) -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    d["virtual_services"][0].update(extra)
    return d


# -- C4: SNI passthrough -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"tls": True},
        {"http2": True},
        {"tls_profile": "modern"},
        {"https_redirect_port": 80},
        {"http3": True},
    ],
)
def test_passthrough_excludes_tls_options(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        dm.VsPort(port=443, tls_passthrough=True, **bad)


def test_passthrough_routes_validate_and_render_maps() -> None:
    d = data({}, PASSTHROUGH)
    assert problems(d) == []
    snap = snapshot_from_mapping(d)
    files = render_haproxy(snap, snap.se_groups[0].id).files
    cfg = files["haproxy/haproxy.cfg"]
    assert "tcp-request content accept if { req.ssl_hello_type 1 }" in cfg
    assert files["haproxy/maps/raw-sni.map"] == "mail.example.test pool_web\n"


def test_tcp_routes_need_passthrough_on_every_port() -> None:
    mixed = {**PASSTHROUGH, "ports": [{"port": 443, "tls_passthrough": True}, {"port": 8443}]}
    with pytest.raises(ValidationError, match="tls_passthrough"):
        snapshot_from_mapping(data({}, mixed))
    plain = {**PASSTHROUGH, "ports": [{"port": 9000}]}
    with pytest.raises(ValidationError, match="routes"):
        snapshot_from_mapping(data({}, plain))


@pytest.mark.parametrize(
    "match",
    [{"path_prefix": "/x"}, {"host": "a.example.test", "path_exact": "/x"}],
)
def test_tcp_routes_match_the_host_only(match: dict[str, Any]) -> None:
    route = {"match": match, "pool_id": "pool-web"}
    d = data({}, {**PASSTHROUGH, "routes": [route]})
    assert problems(d) == [("virtual_services.raw.routes[0]", "passthrough_route_invalid")]


def test_passthrough_is_not_valid_on_http_or_udp_services() -> None:
    with pytest.raises(ValidationError, match="tls_passthrough"):
        snapshot_from_mapping(service(ports=[{"port": 443, "tls_passthrough": True}]))
    d = data({}, {**UDP_SERVICE, "ports": [{"port": 53, "tls_passthrough": True}]})
    assert ("virtual_services.dns.ports[0].tls_passthrough", "udp_field_not_allowed") in problems(d)


# -- C1: HTTP/3, backend HTTP/2, tunnel timeout --------------------------------------------------


def test_http3_needs_tls_and_http2() -> None:
    for bad in ({"tls": True}, {"http2": True}, {}):
        with pytest.raises(ValidationError, match="http3"):
            dm.VsPort(port=443, http3=True, **bad)
    assert dm.VsPort(port=443, tls=True, http2=True, http3=True).http3


def test_http3_renders_a_quic_listener_and_alt_svc() -> None:
    d = service(ports=[{"port": 443, "tls": True, "http2": True, "http3": True}])
    snap = snapshot_from_mapping(d)
    cfg = render_haproxy(snap, snap.se_groups[0].id).files["haproxy/haproxy.cfg"]
    assert "bind quic4@192.0.2.20:443 ssl crt-list" in cfg
    assert "alpn h3" in cfg
    assert "alt-svc 'h3=\":443\"; ma=86400'" in cfg


def test_http3_is_refused_on_tcp_and_udp_services() -> None:
    with pytest.raises(ValidationError):
        snapshot_from_mapping(data({}, {**TCP_SERVICE, "ports": [{"port": 443, "http3": True}]}))
    with pytest.raises(ValidationError):
        snapshot_from_mapping(data({}, {**UDP_SERVICE, "ports": [{"port": 53, "http3": True}]}))


@pytest.mark.parametrize(
    ("kwargs", "ok"),
    [
        ({"backend_protocol": "h2", "tls": True}, True),
        ({"backend_protocol": "h2"}, False),
        ({"backend_protocol": "h2c"}, True),
        ({"backend_protocol": "h2c", "tls": True}, False),
        ({"backend_protocol": "spdy"}, False),
        ({"tunnel_timeout_s": 3600}, True),
        ({"tunnel_timeout_s": 0}, False),
    ],
)
def test_backend_protocol_and_tunnel_timeout(kwargs: dict[str, Any], ok: bool) -> None:
    if ok:
        dm.Pool.model_validate(pool_payload(**kwargs))
    else:
        with pytest.raises(ValidationError):
            dm.Pool.model_validate(pool_payload(**kwargs))


def test_backend_protocol_needs_http_services() -> None:
    d = data({"backend_protocol": "h2c"}, TCP_SERVICE)
    assert problems(d) == [("pools.web.backend_protocol", "backend_protocol_needs_http")]
    assert problems(data({"backend_protocol": "h2c"})) == []
    udp = data({"backend_protocol": "h2c", "tunnel_timeout_s": 5}, UDP_SERVICE)
    udp["pools"][0]["members"] = [{"address": "10.20.0.5", "port": 53}]
    got = problems(udp)
    assert ("pools.web.backend_protocol", "udp_field_not_allowed") in got
    assert ("pools.web.tunnel_timeout_s", "udp_field_not_allowed") in got


def test_backend_lines() -> None:
    d = data({"backend_protocol": "h2c", "tunnel_timeout_s": 600})
    snap = snapshot_from_mapping(d)
    cfg = render_haproxy(snap, snap.se_groups[0].id).files["haproxy/haproxy.cfg"]
    assert "timeout tunnel 600s" in cfg
    assert " proto h2 " in cfg


# -- C5: compression --------------------------------------------------------------------------


def test_compression_defaults_and_bounds() -> None:
    assert dm.Compression().algorithms == ("gzip",)
    bad: dict[str, Any]
    for bad in (
        {"algorithms": []},
        {"algorithms": ["gzip", "gzip"]},
        {"algorithms": ["brotli"]},
        {"content_types": ["Text/HTML"]},
        {"content_types": ["text/html; charset=x"]},
        {"content_types": []},
    ):
        with pytest.raises(ValidationError):
            dm.Compression.model_validate(bad)


def test_compression_renders_and_is_http_only() -> None:
    d = service(compression={"algorithms": ["gzip", "deflate"], "content_types": ["text/css"]})
    snap = snapshot_from_mapping(d)
    cfg = render_haproxy(snap, snap.se_groups[0].id).files["haproxy/haproxy.cfg"]
    assert "compression algo gzip deflate" in cfg
    assert "compression type text/css" in cfg
    with pytest.raises(ValidationError, match="compression"):
        snapshot_from_mapping(data({}, {**TCP_SERVICE, "compression": {}}))
    d = data({}, {**UDP_SERVICE, "compression": {}})
    assert ("virtual_services.dns.compression", "udp_field_not_allowed") in problems(d)


def test_the_renderer_refuses_an_unsafe_media_type() -> None:
    snap = snapshot_from_mapping(service(compression={}))
    bad = dm.Compression.model_construct(algorithms=("gzip",), content_types=("text/html x",))
    svc = snap.virtual_services[0].model_copy(update={"compression": bad})
    broken = snap.model_copy(update={"virtual_services": (svc,)})
    with pytest.raises(RenderError, match="media type"):
        render_haproxy(broken, broken.se_groups[0].id)


# -- C3: persistence table sync ----------------------------------------------------------------


def with_peers(*members: dict[str, str], **extra: Any) -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    d["pools"][0]["persistence"] = {"type": "source_ip"}
    d["se_groups"][0]["peers"] = {"members": list(members), **extra}
    return d


A = {"name": "se-a", "address": "10.20.0.5"}
B = {"name": "se-b", "address": "10.20.0.6"}


def test_peers_render_a_section_and_attach_it_to_the_tables() -> None:
    snap = snapshot_from_mapping(with_peers(B, A))
    assert validate_snapshot(snap) == []
    cfg = render_haproxy(snap, snap.se_groups[0].id).files["haproxy/haproxy.cfg"]
    assert "peers sy_peers\n    peer se-a 10.20.0.5:10000\n    peer se-b 10.20.0.6:10000\n" in cfg
    assert "expire 1800s peers sy_peers" in cfg


def test_no_peers_means_local_tables() -> None:
    d = with_peers(A, B)
    del d["se_groups"][0]["peers"]
    snap = snapshot_from_mapping(d)
    cfg = render_haproxy(snap, snap.se_groups[0].id).files["haproxy/haproxy.cfg"]
    assert "peers" not in cfg


@pytest.mark.parametrize(
    "members",
    [
        [A],
        [A, {"name": "se-a", "address": "10.20.0.7"}],
        [A, {"name": "se-c", "address": "10.20.0.5"}],
    ],
)
def test_peer_lists_are_validated(members: list[dict[str, str]]) -> None:
    with pytest.raises(ValidationError):
        snapshot_from_mapping(with_peers(*members))


def test_peers_must_sit_on_a_backend_network_of_the_group() -> None:
    outside = {"name": "se-c", "address": "172.16.0.9"}
    got = [
        (p.path, p.code) for p in validate_snapshot(snapshot_from_mapping(with_peers(A, outside)))
    ]
    assert got == [("se_groups.edge.peers.members[1].address", "peer_outside_backend_network")]


# -- C2: weighted pools ------------------------------------------------------------------------


def weighted(*weights: int, match: dict[str, Any] | None = None) -> dict[str, Any]:
    pools = [{"pool_id": f"pool-{i}", "weight": w} for i, w in enumerate(weights)]
    return {"match": match or {"host": "split.example.test"}, "pools": pools}


def with_weighted_route(route: dict[str, Any]) -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    d["pools"] += [
        {
            "id": f"pool-{i}",
            "tenant_id": "t-acme",
            "name": f"w{i}",
            "members": [{"address": f"10.20.0.{20 + i}", "port": 80}],
        }
        for i in range(3)
    ]
    d["certificates"][0]["sans"].append("split.example.test")
    d["virtual_services"][0]["routes"].append(route)
    return d


@pytest.mark.parametrize(
    "weights",
    [(90,), (50, 40), (60, 50), (100, 0), (1, 1, 98, 0)],
)
def test_weighted_routes_need_two_pools_adding_up_to_100(weights: tuple[int, ...]) -> None:
    with pytest.raises(ValidationError):
        dm.VsRoute.model_validate(weighted(*weights))


def test_a_route_has_exactly_one_target() -> None:
    one = "00000000-0000-0000-0000-000000000001"
    two = "00000000-0000-0000-0000-000000000002"
    pools = [{"pool_id": one, "weight": 50}, {"pool_id": two, "weight": 50}]
    match = {"host": "a.example.test"}
    with pytest.raises(ValidationError, match="exactly one"):
        dm.VsRoute.model_validate({"match": match, "pools": pools, "pool_id": one})
    with pytest.raises(ValidationError, match="exactly one"):
        dm.VsRoute.model_validate({"match": match})


def test_the_same_pool_cannot_appear_twice() -> None:
    pools = [{"pool_id": "00000000-0000-0000-0000-000000000001", "weight": w} for w in (50, 50)]
    with pytest.raises(ValidationError, match="only once"):
        dm.VsRoute.model_validate({"match": {"host": "a.example.test"}, "pools": pools})


def test_weighted_routes_validate_render_and_reference_every_pool() -> None:
    d = with_weighted_route(weighted(70, 30))
    assert problems(d) == []
    snap = snapshot_from_mapping(d)
    group = snap.se_groups[0].id
    cfg = render_haproxy(snap, group).files["haproxy/haproxy.cfg"]
    assert "http-request set-var(txn.pick) rand(100)" in cfg
    assert "{ var(txn.pick) -m int lt 70 }" in cfg
    assert {p.name for p in snap.for_se_group(group).pools} >= {"w0", "w1"}
    assert "w2" not in {p.name for p in snap.for_se_group(group).pools}


def test_a_weighted_route_to_a_missing_pool_is_reported_at_the_pool() -> None:
    d = with_weighted_route(weighted(70, 30))
    d["virtual_services"][0]["routes"][-1]["pools"][1]["pool_id"] = "pool-missing"
    assert problems(d) == [("virtual_services.web.routes[1].pools[1].pool_id", "unknown_reference")]


def test_tcp_routes_cannot_be_weighted() -> None:
    route = {"match": {"host": "a.example.test"}, "pools": weighted(50, 50)["pools"]}
    d = with_weighted_route(route)
    d["virtual_services"].append(
        {
            **PASSTHROUGH,
            "id": "vs-sni",
            "tenant_id": "t-acme",
            "se_group_id": "seg-1",
            "routes": [
                {
                    **route,
                    "pools": [
                        {"pool_id": "pool-0", "weight": 50},
                        {"pool_id": "pool-1", "weight": 50},
                    ],
                }
            ],
            "default_pool_id": "pool-0",
            "vip": "192.0.2.43",
        }
    )
    assert ("virtual_services.raw.routes[0]", "passthrough_route_invalid") in problems(d)


# -- C2: priority groups -----------------------------------------------------------------------


def chain_data(*, monitors: bool = True) -> dict[str, Any]:
    route = {
        "match": {"host": "split.example.test"},
        "pool_id": "pool-0",
        "fallback_pool_ids": ["pool-1", "pool-2"],
    }
    d = with_weighted_route(route)
    d["health_monitors"] = [
        {"id": "hm", "tenant_id": "t-acme", "name": "hm", "type": "tcp"},
    ]
    if monitors:
        for pool in d["pools"]:
            if pool["id"] in ("pool-0", "pool-1"):
                pool["health_monitor_id"] = "hm"
    return d


def test_a_priority_chain_renders_nbsrv_conditions_in_order() -> None:
    d = chain_data()
    assert problems(d) == []
    snap = snapshot_from_mapping(d)
    cfg = render_haproxy(snap, snap.se_groups[0].id).files["haproxy/haproxy.cfg"]
    lines = [line.strip() for line in cfg.splitlines() if "nbsrv" in line]
    assert [x.split()[1] for x in lines] == ["pool_w0", "pool_w1"]
    assert "{ nbsrv(pool_w0) gt 0 }" in lines[0]
    last = next(line for line in cfg.splitlines() if "use_backend pool_w2" in line)
    assert "nbsrv" not in last
    group = snap.se_groups[0].id
    assert {p.name for p in snap.for_se_group(group).pools} >= {"w0", "w1", "w2"}


def test_a_chain_needs_health_monitors_on_all_but_the_last_pool() -> None:
    got = problems(chain_data(monitors=False))
    assert got == [
        ("virtual_services.web.routes[1].fallback_pool_ids", "failover_needs_health_monitor"),
        ("virtual_services.web.routes[1].fallback_pool_ids", "failover_needs_health_monitor"),
    ]


def test_chain_shape_is_validated() -> None:
    one = "00000000-0000-0000-0000-000000000001"
    two = "00000000-0000-0000-0000-000000000002"
    match = {"host": "a.example.test"}
    with pytest.raises(ValidationError, match="only once"):
        dm.VsRoute.model_validate({"match": match, "pool_id": one, "fallback_pool_ids": [one]})
    redirect = {"location": "https://x.example.test/", "code": 301}
    with pytest.raises(ValidationError, match="needs pool_id"):
        dm.VsRoute.model_validate(
            {"match": match, "redirect": redirect, "fallback_pool_ids": [two]}
        )
    with pytest.raises(ValidationError):
        dm.VsRoute.model_validate(
            {"match": match, "pool_id": one, "fallback_pool_ids": [two, one, two, one]}
        )


# -- C5: cache ---------------------------------------------------------------------------------


def test_cache_renders_a_section_and_the_lines_after_the_policies() -> None:
    d = service(cache={"size_mb": 32, "max_object_kb": 256, "max_age_s": 30})
    snap = snapshot_from_mapping(d)
    assert validate_snapshot(snap) == []
    cfg = render_haproxy(snap, snap.se_groups[0].id).files["haproxy/haproxy.cfg"]
    assert (
        "cache sy_cache_web\n    total-max-size 32\n    max-object-size 262144\n    max-age 30"
        in cfg
    )
    assert cfg.index("http-request cache-use sy_cache_web") > cfg.index("set-var(txn.host)")
    assert cfg.index("cache-use") < cfg.index("use_backend")


@pytest.mark.parametrize(
    "bad", [{"size_mb": 0}, {"size_mb": 257}, {"max_object_kb": 9000}, {"max_age_s": 0}]
)
def test_cache_bounds(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        dm.CacheSettings.model_validate(bad)


def test_cache_is_http_only_and_shares_a_group_quota() -> None:
    with pytest.raises(ValidationError, match="cache"):
        snapshot_from_mapping(data({}, {**TCP_SERVICE, "cache": {}}))
    d = service(cache={"size_mb": 256})
    for i in range(4):
        extra = copy.deepcopy(d["virtual_services"][0])
        extra.update(id=f"vs-c{i}", name=f"c{i}", vip=f"192.0.2.{30 + i}")
        d["virtual_services"].append(extra)
    assert problems(d) == [("se_groups.edge", "cache_quota_exceeded")]
