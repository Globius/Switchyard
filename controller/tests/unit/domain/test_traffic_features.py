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
