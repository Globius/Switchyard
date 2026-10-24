# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Behavioural tests for the HAProxy renderer (details the golden files do not isolate)."""

import random
import re
import uuid
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from switchyard.domain.errors import DomainValidationError
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.domain.models import Algorithm, Pool, RedirectAction, RouteMatch
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import _Renderer, render_haproxy
from tests.factories import base_data

GROUP = uuid.UUID(symbolic_uuid("seg-1"))
CRT = "/etc/switchyard/certs/web.crtlist"


def render(data: dict[str, Any]) -> dict[str, str]:
    return dict(render_haproxy(snapshot_from_mapping(data), GROUP).files)


def cfg(data: dict[str, Any]) -> str:
    return render(data)["haproxy/haproxy.cfg"]


def lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()]


def backend_block(text: str, name: str) -> str:
    """The indented lines of one ``backend`` section (empty when it is not rendered)."""
    match = re.search(rf"^backend {name}\n((?:[ \t]+\S.*\n)*)", text, re.M)
    assert match, f"no backend {name} in:\n{text}"
    return match.group(1)


def with_routes(routes: list[dict[str, Any]], **vs: Any) -> dict[str, Any]:
    data = base_data()
    data["virtual_services"][0]["routes"] = routes
    data["virtual_services"][0].update(vs)
    return data


class TestDeterminism:
    def test_input_order_does_not_change_output(self) -> None:
        data = base_data()
        data["pools"][0]["members"] = [
            {"address": "10.20.0.12", "port": 8080},
            {"address": "10.20.0.11", "port": 8080},
            {"address": "10.20.0.13", "port": 8080, "weight": 10},
        ]
        data["pools"].append(
            {
                "id": "pool-b",
                "tenant_id": "t-acme",
                "name": "aaa",
                "members": [{"address": "10.20.0.50", "port": 80}],
            }
        )
        data["virtual_services"][0]["routes"] = [
            {"match": {"host": "z.example.test"}, "pool_id": "pool-web"},
            {"match": {"host": "a.example.test"}, "pool_id": "pool-b"},
            {"match": {"host": "*.example.test"}, "pool_id": "pool-web"},
        ]
        expected = render(data)
        for seed in range(10):
            rnd = random.Random(seed)  # noqa: S311 - test shuffling, not security
            shuffled = {k: list(v) for k, v in data.items()}
            for key in ("pools", "networks", "certificates", "virtual_services"):
                rnd.shuffle(shuffled[key])
            rnd.shuffle(shuffled["pools"][0]["members"])
            rnd.shuffle(shuffled["virtual_services"][0]["routes"])
            rnd.shuffle(shuffled["virtual_services"][0]["ports"])
            assert render(shuffled) == expected

    def test_content_hash_tracks_content(self) -> None:
        snap = snapshot_from_mapping(base_data())
        first = render_haproxy(snap, GROUP)
        assert first.content_hash == render_haproxy(snap, GROUP).content_hash
        changed = base_data()
        changed["pools"][0]["members"][0]["weight"] = 7
        second = render_haproxy(snapshot_from_mapping(changed), GROUP)
        assert second.content_hash != first.content_hash

    def test_output_is_lf_and_ends_with_newline(self) -> None:
        for content in render(base_data()).values():
            assert "\r" not in content
            assert content.endswith("\n")

    def test_no_timestamps_or_random_ids(self) -> None:
        text = cfg(base_data())
        assert not re.search(r"\d{4}-\d{2}-\d{2}", text)
        assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", text)


@settings(max_examples=40)
@given(
    st.lists(
        st.tuples(
            st.from_regex(r"[a-z][a-z0-9]{0,8}", fullmatch=True),
            st.booleans(),
        ),
        min_size=1,
        max_size=8,
        unique_by=lambda t: (t[0], t[1]),
    )
)
def test_each_route_host_appears_in_exactly_one_map(hosts: list[tuple[str, bool]]) -> None:
    routes: list[dict[str, Any]] = [
        {"match": {"host": ("*." if wild else "") + f"{label}.example.test"}, "pool_id": "pool-web"}
        for label, wild in hosts
    ]
    data = with_routes(routes)
    data["certificates"][0]["sans"] = sorted({r["match"]["host"] for r in routes})
    files = render(data)
    maps = {k: v for k, v in files.items() if k.startswith("haproxy/maps/")}
    for label, wild in hosts:
        needle = f"^[^.]+[.]{label}[.]example[.]test$" if wild else f"{label}.example.test"
        found = [
            name
            for name, text in maps.items()
            if any(line.split()[0] == needle for line in text.splitlines())
        ]
        assert len(found) == 1, (needle, found)


class TestRouting:
    def test_exact_before_wildcard_before_default(self) -> None:
        data = with_routes(
            [
                {"match": {"host": "*.example.test"}, "pool_id": "pool-web"},
                {"match": {"host": "app.example.test"}, "pool_id": "pool-web"},
            ],
            default_pool_id="pool-web",
        )
        rules = [
            line for line in lines(cfg(data)) if line.startswith(("use_backend", "default_backend"))
        ]
        assert [r.split()[1] for r in rules] == [
            "%[var(txn.host),map(/etc/switchyard/haproxy/maps/web-host.map)]",
            "%[var(txn.host),map_reg(/etc/switchyard/haproxy/maps/web-wild.map)]",
            "pool_web",
        ]

    def test_map_rules_are_guarded_by_found(self) -> None:
        text = cfg(with_routes([{"match": {"host": "app.example.test"}, "pool_id": "pool-web"}]))
        rule = next(line for line in lines(text) if line.startswith("use_backend %["))
        assert rule.endswith("-m found }")

    def test_path_routes_run_before_host_maps_in_order(self) -> None:
        data = with_routes(
            [
                {"match": {"host": "app.example.test"}, "pool_id": "pool-web"},
                {"match": {"path_prefix": "/b"}, "pool_id": "pool-web", "order": 2},
                {"match": {"path_prefix": "/a"}, "pool_id": "pool-web", "order": 1},
            ]
        )
        rules = [line for line in lines(cfg(data)) if line.startswith("use_backend")]
        assert "path_beg /a" in rules[0]
        assert "path_beg /b" in rules[1]
        assert "map(" in rules[2]

    def test_wildcard_host_condition_is_single_quoted_regex(self) -> None:
        data = with_routes(
            [{"match": {"host": "*.example.test", "path_prefix": "/x"}, "pool_id": "pool-web"}]
        )
        assert "{ var(txn.host) -m reg '^[^.]+[.]example[.]test$' }" in cfg(data)

    def test_no_default_backend_when_not_configured(self) -> None:
        assert "default_backend" not in cfg(base_data())

    def test_map_files_sorted_and_only_written_when_used(self) -> None:
        data = with_routes(
            [
                {"match": {"host": "b.example.test"}, "pool_id": "pool-web"},
                {"match": {"host": "a.example.test"}, "pool_id": "pool-web"},
            ]
        )
        files = render(data)
        assert (
            files["haproxy/maps/web-host.map"]
            == "a.example.test pool_web\nb.example.test pool_web\n"
        )
        assert "haproxy/maps/web-wild.map" not in files


class TestRedirects:
    def test_https_redirect_uses_host_variable_and_port(self) -> None:
        data = base_data()
        data["virtual_services"][0]["ports"] = [
            {"port": 8443, "tls": True},
            {"port": 8080, "https_redirect_port": 8443},
        ]
        text = cfg(data)
        assert (
            'http-request redirect prefix "https://%[var(txn.host)]:8443" code 301 '
            "if { dst_port 8080 }"
        ) in text

    def test_policy_set_redirects_precede_route_redirects_and_routing(self) -> None:
        data = base_data()
        data["policy_sets"] = [
            {
                "id": "ps-1",
                "tenant_id": "t-acme",
                "name": "dav",
                "kind": "redirect",
                "rules": [
                    {
                        "match": {"path_prefix": "/.well-known/carddav"},
                        "redirect": {"location": "/dav/"},
                    }
                ],
            }
        ]
        vs = data["virtual_services"][0]
        vs["policy_set_ids"] = ["ps-1"]
        vs["routes"] = [
            {"match": {"host": "app.example.test"}, "pool_id": "pool-web"},
            {"match": {"path_exact": "/old"}, "redirect": {"location": "/new", "code": 302}},
        ]
        rules = [
            line
            for line in lines(cfg(data))
            if "redirect location" in line or line.startswith("use_backend")
        ]
        assert (
            rules[0]
            == "http-request redirect location /dav/ code 301 if { path_beg /.well-known/carddav }"
        )
        assert rules[1] == "http-request redirect location /new code 302 if { path /old }"
        assert rules[2].startswith("use_backend")

    def test_percent_in_location_is_doubled(self) -> None:
        data = with_routes(
            [{"match": {"path_exact": "/a"}, "redirect": {"location": "/b%20c"}}],
            default_pool_id="pool-web",
        )
        assert "redirect location /b%%20c code 301" in cfg(data)


class TestClientAddress:
    def data(self, **vs: Any) -> dict[str, Any]:
        data = base_data()
        data["policy_sets"] = [
            {
                "id": "ps-cf",
                "tenant_id": "t-acme",
                "name": "cf",
                "kind": "allow_list",
                "cidrs": ["198.51.100.0/24"],
            }
        ]
        data["virtual_services"][0].update(allow_list_id="ps-cf", **vs)
        return data

    def test_restore_only_when_source_in_allow_list(self) -> None:
        text = cfg(self.data(restore_client_ip_header="CF-Connecting-IP"))
        rules = [r for r in lines(text) if r.startswith("http-request")]
        trusted = next(i for i, r in enumerate(rules) if "set-var(txn.trusted)" in r)
        set_src = next(i for i, r in enumerate(rules) if "set-src" in r)
        assert trusted < set_src, "trust must be decided before set-src changes `src`"
        assert (
            "if { var(txn.trusted) -m bool } { hdr(CF-Connecting-IP) -m found }" in rules[set_src]
        )
        assert any(
            r == "http-request del-header CF-Connecting-IP if !{ var(txn.trusted) -m bool }"
            for r in rules
        )

    def test_no_restore_rules_without_header(self) -> None:
        text = cfg(self.data())
        assert "set-src" not in text
        assert "txn.trusted" not in text

    def test_enforcement_rejects_unlisted_sources(self) -> None:
        text = cfg(self.data(enforce_allow_list=True))
        assert (
            "tcp-request session reject if !{ src -f /etc/switchyard/haproxy/lists/cf.cidr }"
            in text
        )

    def test_no_enforcement_by_default(self) -> None:
        assert "tcp-request" not in cfg(self.data())

    def test_allow_list_file_is_sorted_and_deduplicated_by_family(self) -> None:
        data = self.data()
        data["policy_sets"][0]["cidrs"] = ["2001:db8::/32", "203.0.113.0/24", "198.51.100.0/24"]
        assert (
            render(data)["haproxy/lists/cf.cidr"]
            == "198.51.100.0/24\n203.0.113.0/24\n2001:db8::/32\n"
        )

    def test_only_http_frontends_add_forwarded_for(self) -> None:
        assert "option forwardfor" in cfg(base_data())
        data = base_data()
        data["virtual_services"][0].update(
            protocol="tcp",
            routes=[],
            default_pool_id="pool-web",
            ports=[{"port": 6443}],
            certificate_ids=[],
        )
        assert "forwardfor" not in cfg(data)

    def test_spoofable_headers_are_always_reset(self) -> None:
        text = cfg(base_data())
        assert "http-request del-header X-Forwarded-For" in text
        assert text.index("del-header X-Forwarded-For") < text.index("set-header X-Forwarded-Proto")


class TestBackends:
    def test_member_states(self) -> None:
        data = base_data()
        data["pools"][0]["members"] = [
            {"address": "10.20.0.11", "port": 80, "admin_state": "drain", "weight": 30},
            {"address": "10.20.0.12", "port": 80, "admin_state": "disabled"},
            {"address": "10.20.0.13", "port": 80},
        ]
        servers = [line for line in lines(cfg(data)) if line.startswith("server")]
        assert servers == [
            "server 10-20-0-11-80 10.20.0.11:80 weight 30 disabled",
            "server 10-20-0-12-80 10.20.0.12:80 weight 100 disabled",
            "server 10-20-0-13-80 10.20.0.13:80 weight 100",
        ]

    def test_member_without_port_follows_client_port(self) -> None:
        data = base_data()
        data["pools"][0]["members"] = [{"address": "10.20.0.11"}]
        assert "server 10-20-0-11 10.20.0.11 weight 100" in cfg(data)

    def test_ipv6_member(self) -> None:
        data = base_data()
        data["pools"][0]["members"] = [{"address": "2001:db8::10", "port": 8080}]
        assert "server 2001-db8--10-8080 ipv6@2001:db8::10:8080 weight 100" in cfg(data)

    def _sni_data(self, **monitor: Any) -> dict[str, Any]:
        data = base_data()
        data["health_monitors"] = [
            {"id": "hm", "tenant_id": "t-acme", "name": "h", "type": "tcp", **monitor}
        ]
        data["pools"][0].update(health_monitor_id="hm", tls=True)
        return data

    def test_check_sni_uses_the_monitor_sni_over_the_route_host(self) -> None:
        text = cfg(self._sni_data(sni="backend.example.org"))
        assert "check check-ssl check-sni backend.example.org ssl" in text

    def test_check_sni_falls_back_to_the_route_host(self) -> None:
        data = self._sni_data()
        assert "check check-ssl check-sni app.example.test ssl" in cfg(data)

    def test_no_check_sni_without_tls_or_a_name(self) -> None:
        data = self._sni_data(sni="backend.example.org")
        data["pools"][0]["tls"] = False
        assert "check-sni" not in cfg(data)

    def test_udp_monitor_rejects_sni(self) -> None:
        with pytest.raises(ValidationError, match="sni is not valid"):
            snapshot_from_mapping(self._sni_data(type="udp", sni="backend.example.org"))

    def test_http_monitor_and_tls_server_options(self) -> None:
        data = base_data()
        data["health_monitors"] = [
            {
                "id": "hm",
                "tenant_id": "t-acme",
                "name": "h",
                "type": "http",
                "interval_s": 7,
                "timeout_s": 2,
                "rise": 4,
                "fall": 5,
                "http": {"path": "/up", "host": "app.example.test", "expect_status": [204, 200]},
            }
        ]
        pool = data["pools"][0]
        pool.update(health_monitor_id="hm", tls=True, tls_verify=True)
        text = cfg(data)
        assert "http-check send meth GET uri /up ver HTTP/1.1 hdr Host app.example.test" in text
        assert "http-check expect status 200,204" in text
        assert "default-server inter 7s fall 5 rise 4" in text
        assert "timeout check 2s" in text
        assert (
            "server 10-20-0-11-8080 10.20.0.11:8080 check check-ssl check-sni app.example.test "
            "ssl verify required ca-file /etc/ssl/certs/ca-certificates.crt "
            "sni req.hdr(host),field(1,:),lower weight 100"
        ) in text

    def test_check_port_override(self) -> None:
        data = base_data()
        data["health_monitors"] = [
            {"id": "hm", "tenant_id": "t-acme", "name": "h", "type": "tcp", "port": 9000}
        ]
        data["pools"][0]["health_monitor_id"] = "hm"
        assert "10.20.0.11:8080 check port 9000 weight 100" in cfg(data)

    def test_persistence(self) -> None:
        data = base_data()
        data["pools"][0]["persistence"] = {"type": "source_ip", "timeout_s": 60, "table_size": 5000}
        text = cfg(data)
        assert "stick-table type ipv6 size 5000 expire 60s" in text
        assert "stick on src" in text

    @pytest.mark.parametrize("seconds", [2, 45, 3600])
    def test_tunnel_timeout_is_the_pools_value_and_only_in_its_backend(self, seconds: int) -> None:
        """The WebSocket idle timeout is the pool field, spelled as HAProxy's ``timeout tunnel``.

        A service does not carry the knob: ``Pool.tunnel_timeout_s`` does, and the rendered line
        must follow it exactly so the tunnel lives as long as the operator asked.
        """
        data = base_data()
        data["pools"][0]["tunnel_timeout_s"] = seconds
        text = cfg(data)
        assert f"timeout tunnel {seconds}s" in lines(backend_block(text, "pool_web"))
        assert text.count(f"timeout tunnel {seconds}s") == 1, "one backend, one line"

    def test_without_the_field_the_backend_keeps_the_default_tunnel_timeout(self) -> None:
        text = cfg(base_data())
        assert "timeout tunnel" not in backend_block(text, "pool_web")
        assert "timeout tunnel 1h" in text, "`defaults` always carries one"

    def test_only_referenced_pools_of_enabled_services_are_rendered(self) -> None:
        data = base_data()
        data["pools"].append({"id": "pool-unused", "tenant_id": "t-acme", "name": "unused"})
        assert "pool_unused" not in cfg(data)


class TestSelection:
    def test_disabled_services_are_not_rendered(self) -> None:
        data = base_data()
        data["virtual_services"][0]["enabled"] = False
        text = cfg(data)
        assert "frontend vs_" not in text
        assert "backend" not in text

    def test_udp_is_skipped_with_a_warning(self) -> None:
        data = base_data()
        data["virtual_services"].append(
            {
                "id": "vs-udp",
                "tenant_id": "t-acme",
                "se_group_id": "seg-1",
                "name": "wg",
                "protocol": "udp",
                "vip": "192.0.2.25",
                "ports": [{"port": 51820}],
                "default_pool_id": "pool-wg",
            }
        )
        data["pools"].append(
            {
                "id": "pool-wg",
                "tenant_id": "t-acme",
                "name": "wg",
                "members": [{"address": "10.20.0.50", "port": 51820}],
            }
        )
        result = render_haproxy(snapshot_from_mapping(data), GROUP)
        assert result.warnings == ()  # IPVS serves it, so skipping it here is not a problem
        assert result.notices == ()
        assert "vs_wg" not in result.files["haproxy/haproxy.cfg"]
        assert "pool_wg" not in result.files["haproxy/haproxy.cfg"]

    def test_other_se_groups_do_not_leak_in(self) -> None:
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
        data["pools"].append(
            {
                "id": "pool-o",
                "tenant_id": "t-acme",
                "name": "other",
                "members": [{"address": "10.20.0.60", "port": 80}],
            }
        )
        data["virtual_services"].append(
            {
                "id": "vs-o",
                "tenant_id": "t-acme",
                "se_group_id": "seg-2",
                "name": "other-svc",
                "protocol": "tcp",
                "vip": "192.0.2.40",
                "ports": [{"port": 9000}],
                "default_pool_id": "pool-o",
            }
        )
        text = cfg(data)
        assert "other" not in text.replace("# SE group: edge", "")

    def test_unknown_group(self) -> None:
        with pytest.raises(RenderError, match="unknown SE group"):
            render_haproxy(snapshot_from_mapping(base_data()), uuid.UUID(int=99))

    def test_invalid_snapshots_are_not_rendered(self) -> None:
        data = base_data()
        data["virtual_services"][0]["vip"] = "198.51.100.1"
        with pytest.raises(DomainValidationError):
            render_haproxy(snapshot_from_mapping(data), GROUP)

    def test_empty_group_renders_valid_skeleton(self) -> None:
        data = base_data()
        data["virtual_services"] = []
        text = cfg(data)
        assert "frontend vs_" not in text
        assert text.startswith("# Managed by Switchyard")


class TestTls:
    def test_crt_list_has_default_certificate_first_and_sorted_snis(self) -> None:
        data = base_data()
        data["certificates"].append(
            {
                "id": "cert-2",
                "tenant_id": "t-acme",
                "name": "second",
                "source": "upload",
                "sans": ["z.other.test", "b.other.test"],
                "not_after": "2038-01-01T00:00:00+00:00",
                "secret_ref": "kv/x/second",
            }
        )
        data["virtual_services"][0]["certificate_ids"] = ["cert-2", "cert-wild"]
        data["virtual_services"][0]["routes"] = [
            {"match": {"host": "b.other.test"}, "pool_id": "pool-web"},
            {"match": {"host": "app.example.test"}, "pool_id": "pool-web"},
        ]
        assert render(data)["certs/web.crtlist"] == (
            "/etc/switchyard/certs/second.pem b.other.test z.other.test\n"
            "/etc/switchyard/certs/wild-example-test.pem *.example.test example.test\n"
        )

    def test_alpn_follows_http2_flag_and_plain_ports_have_none(self) -> None:
        data = base_data()
        data["virtual_services"][0]["ports"] = [
            {"port": 443, "tls": True, "http2": True},
            {"port": 8443, "tls": True},
            {"port": 80},
        ]
        binds = [
            line
            for line in lines(cfg(data))
            if line.startswith("bind") and "127.0.0.1:8404" not in line
        ]
        assert binds == [
            "bind 192.0.2.20:80",
            f"bind 192.0.2.20:443 ssl crt-list {CRT} alpn h2,http/1.1,acme-tls/1",
            f"bind 192.0.2.20:8443 ssl crt-list {CRT} alpn http/1.1,acme-tls/1",
        ]

    def test_tcp_service_with_tls_and_proxy_protocol(self) -> None:
        data = base_data()
        data["virtual_services"][0].update(
            protocol="tcp",
            routes=[],
            default_pool_id="pool-web",
            ports=[{"port": 443, "tls": True, "proxy_protocol": True}],
        )
        text = cfg(data)
        assert f"bind 192.0.2.20:443 ssl crt-list {CRT} accept-proxy" in text
        assert "option tcplog" in text
        assert "alpn" not in text


def golden_like() -> dict[str, Any]:
    """Base data plus features that add rule kinds: redirects, persistence, monitors, TLS pool."""
    data = base_data()
    data["health_monitors"] = [
        {"id": "hm", "tenant_id": "t-acme", "name": "h", "type": "http", "http": {"path": "/"}}
    ]
    data["pools"][0].update(health_monitor_id="hm", tls=True, persistence={"type": "source_ip"})
    data["virtual_services"][0]["routes"].append(
        {"match": {"path_prefix": "/x"}, "redirect": {"location": "/y"}}
    )
    return data


class TestBalance:
    """Hash-based balancing: exactly one ``balance`` line per algorithm (ADR 0224)."""

    @pytest.mark.parametrize(
        ("algorithm", "key", "line"),
        [
            ("roundrobin", None, "balance roundrobin"),
            ("leastconn", None, "balance leastconn"),
            ("source", None, "balance source"),
            ("random", None, "balance random"),
            ("uri", None, "balance uri"),
            ("hdr", "X-Cache-Key", "balance hdr(X-Cache-Key)"),
            ("url_param", "tenant_id", "balance url_param tenant_id"),
        ],
    )
    def test_balance_line(self, algorithm: str, key: str | None, line: str) -> None:
        data = base_data()
        data["pools"][0].update(algorithm=algorithm, hash_key=key)
        text = cfg(data)
        assert line in lines(text)
        # hash-type stays at HAProxy's default, so it is never written (ADR 0224).
        assert "hash-type" not in text

    def test_a_hash_algorithm_without_a_key_fails_the_render(self) -> None:
        pool = Pool(
            tenant_id=uuid.UUID(int=2), id=uuid.UUID(int=1), name="cache", algorithm=Algorithm.HDR
        )
        with pytest.raises(RenderError, match="hdr hash key"):
            _Renderer._balance(pool)

    def test_an_unsafe_key_fails_the_render(self) -> None:
        pool = Pool(
            tenant_id=uuid.UUID(int=2),
            id=uuid.UUID(int=1),
            name="cache",
            algorithm=Algorithm.URL_PARAM,
        )
        unsafe = pool.model_copy(update={"hash_key": "x y"})
        with pytest.raises(RenderError, match="unsafe hash key"):
            _Renderer._balance(unsafe)


class TestInjection:
    """Values that bypass pydantic (``model_construct``) must still never reach the config."""

    @pytest.mark.parametrize(
        "bad",
        [
            "x\nlisten evil",
            "x y",
            "x;y",
            "x#y",
            'x"y',
            "x$HOME",
            "x\\y",
            "${PATH}",
            "x\ty",
            "x\x00y",
        ],
    )
    def test_hostile_pool_name(self, bad: str) -> None:
        snap = snapshot_from_mapping(base_data())
        pool = snap.pools[0].model_construct(**{**dict(snap.pools[0]), "name": bad})
        evil = snap.model_copy(update={"pools": (pool,)})
        with pytest.raises(RenderError):
            render_haproxy(evil, GROUP)

    @pytest.mark.parametrize(
        "bad", ["a.example.test\nx", "a b.test", "*.example.test;", "A.TEST", "a.test#"]
    )
    def test_hostile_route_host(self, bad: str) -> None:
        snap = snapshot_from_mapping(base_data())
        vs = snap.virtual_services[0]
        route = vs.routes[0].model_copy(update={"match": RouteMatch.model_construct(host=bad)})
        evil_vs = vs.model_copy(update={"routes": (route,)})
        evil = snap.model_copy(update={"virtual_services": (evil_vs,)})
        # certificate coverage fails first for hosts nothing covers; both outcomes stop the render
        with pytest.raises((RenderError, DomainValidationError)):
            render_haproxy(evil, GROUP)

    @pytest.mark.parametrize("bad", ["/a b", "/a\nhttp-request deny", "/a'b", "/a#b", "/${X}"])
    def test_hostile_path(self, bad: str) -> None:
        data = base_data()
        data["virtual_services"][0]["routes"] = [
            {"match": {"path_prefix": "/ok"}, "pool_id": "pool-web"}
        ]
        snap = snapshot_from_mapping(data)
        vs = snap.virtual_services[0]
        match = RouteMatch.model_construct(host=None, path_prefix=bad, path_exact=None)
        route = vs.routes[0].model_copy(update={"match": match})
        evil = snap.model_copy(
            update={"virtual_services": (vs.model_copy(update={"routes": (route,)}),)}
        )
        with pytest.raises(RenderError):
            render_haproxy(evil, GROUP)

    @pytest.mark.parametrize("bad", ["/a b", "/a\nx", '/a"', "/a#", "/$X", "/a\\b"])
    def test_hostile_redirect_location(self, bad: str) -> None:
        data = base_data()
        data["virtual_services"][0]["routes"] = [
            {"match": {"path_exact": "/old"}, "redirect": {"location": "/new"}}
        ]
        data["virtual_services"][0]["default_pool_id"] = "pool-web"
        snap = snapshot_from_mapping(data)
        vs = snap.virtual_services[0]
        redirect = RedirectAction.model_construct(location=bad, code=301)
        route = vs.routes[0].model_copy(update={"redirect": redirect})
        evil = snap.model_copy(
            update={"virtual_services": (vs.model_copy(update={"routes": (route,)}),)}
        )
        with pytest.raises(RenderError):
            render_haproxy(evil, GROUP)

    def test_hostile_header_name(self) -> None:
        data = base_data()
        data["policy_sets"] = [
            {
                "id": "ps",
                "tenant_id": "t-acme",
                "name": "cf",
                "kind": "allow_list",
                "cidrs": ["198.51.100.0/24"],
            }
        ]
        data["virtual_services"][0].update(allow_list_id="ps", restore_client_ip_header="X-Real-IP")
        snap = snapshot_from_mapping(data)
        vs = snap.virtual_services[0].model_copy(
            update={"restore_client_ip_header": "X-Real-IP\nhttp-request deny"}
        )
        evil = snap.model_copy(update={"virtual_services": (vs,)})
        with pytest.raises(RenderError):
            render_haproxy(evil, GROUP)

    def test_hostile_certificate_name_and_san(self) -> None:
        snap = snapshot_from_mapping(base_data())
        cert = snap.certificates[0]
        for update in ({"name": "x\nbind 0.0.0.0:22"}, {"sans": ("a.test\nx",)}):
            evil_cert = cert.model_copy(update=update)
            evil = snap.model_copy(update={"certificates": (evil_cert,)})
            with pytest.raises((RenderError, DomainValidationError)):
                render_haproxy(evil, GROUP)

    def test_generated_config_never_contains_unexpected_directives(self) -> None:
        """Every non-blank line starts with a known HAProxy keyword."""
        known = {
            "global",
            "defaults",
            "frontend",
            "backend",
            "log",
            "maxconn",
            "stats",
            "ssl-default-bind-options",
            "ssl-default-server-options",
            "option",
            "retries",
            "timeout",
            "mode",
            "bind",
            "tcp-request",
            "http-request",
            "use_backend",
            "default_backend",
            "balance",
            "stick-table",
            "stick",
            "http-check",
            "default-server",
            "server",
            "monitor-uri",
            "no",
        }
        for path, content in render(golden_like()).items():
            if path != "haproxy/haproxy.cfg":
                continue
            for line in content.splitlines():
                word = line.strip().split(" ")[0]
                if not word or word.startswith("#"):
                    continue
                assert word in known, line


class TestFingerprint:
    def test_fingerprint_is_embedded_and_matches_files(self) -> None:
        from switchyard.render.haproxy import FINGERPRINT_LINE, fingerprint_of

        result = render_haproxy(snapshot_from_mapping(base_data()), GROUP)
        cfg_text = result.files["haproxy/haproxy.cfg"]
        assert fingerprint_of(cfg_text) == result.fingerprint
        assert len(FINGERPRINT_LINE.findall(cfg_text)) == 1

    def test_fingerprint_changes_when_any_file_changes(self) -> None:
        first = render_haproxy(snapshot_from_mapping(base_data()), GROUP).fingerprint
        data = base_data()
        data["virtual_services"][0]["routes"][0]["match"]["host"] = "other.example.test"
        assert render_haproxy(snapshot_from_mapping(data), GROUP).fingerprint != first

    def test_fingerprint_survives_data_plane_api_rewrite(self) -> None:
        from switchyard.render.haproxy import fingerprint_of

        # the Data Plane API keeps comments but adds its own header and re-indents
        rewritten = (
            "# _md5hash=1\n# _version=2\n# switchyard-render: sha256:"
            + "a" * 64
            + "\n\nglobal\n  maxconn 1\n"
        )
        assert fingerprint_of(rewritten) == "a" * 64

    def test_no_fingerprint_in_unmanaged_config(self) -> None:
        from switchyard.render.haproxy import fingerprint_of

        assert fingerprint_of("global\n  maxconn 1\n") is None
