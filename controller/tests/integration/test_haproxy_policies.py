"""Real requests through the rendered match model, rewrite policies and fixed responses.

The golden cases prove syntax (``haproxy -c``); this proves what the rules do to traffic. Same lab
as ``test_haproxy_behavior``: 192.0.2.20 edge, backends 192.0.2.51-53 that echo what they
received, clients 192.0.2.98 and 192.0.2.99.
"""

from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest

from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.render.haproxy import render_haproxy
from tests.haproxy_check import HAPROXY_IMAGE
from tests.integration.test_haproxy_behavior import EDGE, EDGE_START, resolve
from tests.lab import BACKEND_IMAGE, SUBNET, Lab

ECHO_CFG = """\
global
    maxconn 100
defaults
    mode http
    timeout client 5s
    timeout server 5s
    timeout connect 5s
frontend f
    bind :8080
    http-request return status 200 content-type text/plain lf-string "be={label} method=%[method] path=%[path] query=%[query] edge=%[req.hdr(x-edge)] cip=%[req.hdr(x-client-ip)] cookie=%[req.hdr(cookie)] via=%[req.hdr(via)] host=%[req.hdr(host)]" hdr Server backend-secret hdr X-Origin {label}
"""

#: Nothing listens on the edge's port 8080, so a pool pointing at it makes HAProxy answer 503.
DEAD = EDGE

#: Records the response headers of a request as one line (``~`` between header lines).
HDRS = r"""
hdrs() { label=$1; shift; out=$(curl -s -k --max-time 6 -D - -o /dev/null "$@" | tr -d '\r' | tr '\n' '~'); echo "$label: $out"; }
"""


def data() -> dict[str, Any]:
    def pool(pid: str, ip: str) -> dict[str, Any]:
        return {
            "id": pid,
            "tenant_id": "t",
            "name": pid,
            "members": [{"address": ip, "port": 8080}],
        }

    def service(vid: str, port: int, **extra: Any) -> dict[str, Any]:
        return {
            "id": vid,
            "tenant_id": "t",
            "se_group_id": "g",
            "name": vid,
            "protocol": "http",
            "vip": EDGE,
            "ports": [{"port": port, "tls": True}],
            "certificate_ids": ["c"],
            "default_pool_id": "a-pool",
            **extra,
        }

    def route(order: int, match: dict[str, Any], pool_id: str) -> dict[str, Any]:
        return {"order": order, "match": match, "pool_id": pool_id}

    return {
        "tenants": [{"id": "t", "name": "acme"}],
        "networks": [{"id": "n", "tenant_id": "t", "name": "lab", "cidr": SUBNET}],
        "se_groups": [
            {
                "id": "g",
                "tenant_id": "t",
                "name": "edge",
                "ha_mode": "vrrp",
                "networks": [{"network_id": "n", "role": "vip"}],
            }
        ],
        "certificates": [
            {
                "id": "c",
                "tenant_id": "t",
                "name": "lab",
                "source": "upload",
                "sans": ["*.example.test", "app.example.test"],
                "not_after": "2038-01-01T00:00:00+00:00",
                "secret_ref": "kv/lab",
            }
        ],
        "pools": [
            pool("a-pool", "192.0.2.51"),
            pool("b-pool", "192.0.2.52"),
            pool("c-pool", "192.0.2.53"),
            pool("dead-pool", DEAD),
        ],
        "policy_sets": [
            {
                "id": "rw",
                "tenant_id": "t",
                "name": "rw",
                "kind": "rewrite",
                "rules": [
                    {"action": {"type": "set_header", "name": "X-Edge", "value": "switchyard"}},
                    {
                        "action": {
                            "type": "set_header",
                            "name": "X-Client-IP",
                            "variable": "client_ip",
                        }
                    },
                    {
                        "match": {"path_prefix": "/internal"},
                        "action": {"type": "remove_header", "name": "Cookie"},
                    },
                    {
                        "match": {"path_prefix": "/api/v1"},
                        "action": {
                            "type": "replace_prefix",
                            "from_prefix": "/api/v1",
                            "to_prefix": "/",
                        },
                    },
                    {
                        "match": {"path_prefix": "/legacy"},
                        "action": {
                            "type": "replace_prefix",
                            "from_prefix": "/legacy",
                            "to_prefix": "/v2",
                        },
                    },
                    {
                        "match": {
                            "methods": ["GET"],
                            "headers": [{"name": "X-Trace", "op": "present"}],
                        },
                        "action": {"type": "add_header", "name": "Via", "value": "1.1 sy"},
                    },
                    {
                        "phase": "response",
                        "action": {
                            "type": "set_header",
                            "name": "Strict-Transport-Security",
                            "value": "max-age=60",
                        },
                    },
                    {"phase": "response", "action": {"type": "remove_header", "name": "Server"}},
                    {
                        "phase": "response",
                        "match": {"path_prefix": "/static"},
                        "action": {
                            "type": "set_header",
                            "name": "Cache-Control",
                            "value": "public",
                        },
                    },
                ],
            },
            {
                "id": "resp",
                "tenant_id": "t",
                "name": "resp",
                "kind": "respond",
                "rules": [
                    {
                        "match": {"path_exact": "/lb-ping"},
                        "status": 200,
                        "content_type": "text/plain",
                        "body": "pong",
                    },
                    {"match": {"path_prefix": "/wp-admin"}, "status": 404},
                    {"match": {"path_exact": "/favicon.ico"}, "status": 204},
                    {
                        "match": {"headers": [{"name": "X-Maint", "op": "equals", "value": "on"}]},
                        "status": 503,
                        "content_type": "text/html",
                        "body": "<h1>maintenance</h1>",
                        "retry_after_s": 30,
                    },
                ],
            },
            {
                "id": "err",
                "tenant_id": "t",
                "name": "err",
                "kind": "error_pages",
                "pages": [
                    {"status": 503, "content_type": "text/html", "body": "<h1>custom 503</h1>"}
                ],
            },
        ],
        "virtual_services": [
            service(
                "match",
                443,
                routes=[
                    route(1, {"methods": ["POST"]}, "b-pool"),
                    route(
                        2,
                        {"headers": [{"name": "X-Canary", "op": "equals", "value": "yes"}]},
                        "b-pool",
                    ),
                    route(
                        3, {"query": [{"name": "canary", "op": "equals", "value": "1"}]}, "b-pool"
                    ),
                    route(4, {"source_cidrs": ["192.0.2.98/32"]}, "c-pool"),
                    route(5, {"sni": "sni.example.test", "path_prefix": "/s"}, "c-pool"),
                    route(
                        6,
                        {
                            "path_prefix": "/h",
                            "headers": [
                                {"name": "X-Pre", "op": "prefix", "value": "ab"},
                                {"name": "X-Gone", "op": "absent"},
                            ],
                        },
                        "b-pool",
                    ),
                ],
            ),
            service("rewrite", 8443, policy_set_ids=["rw"]),
            service(
                "respond",
                9443,
                policy_set_ids=["resp", "err"],
                default_pool_id="dead-pool",
                routes=[route(1, {"path_prefix": "/ok"}, "a-pool")],
            ),
        ],
    }


@pytest.fixture(scope="module")
def lab(lab_network: Lab) -> Iterator[Lab]:
    with lab_network.scope():
        for label, ip in (("A", "192.0.2.51"), ("B", "192.0.2.52"), ("C", "192.0.2.53")):
            lab_network._run(
                BACKEND_IMAGE,
                ip,
                ["haproxy", "-f", "/be.cfg"],
                {"be.cfg": ECHO_CFG.format(label=label)},
            )
        snapshot = snapshot_from_mapping(data())
        rendered = render_haproxy(snapshot, UUID(symbolic_uuid("g")))
        files = {f"etc/switchyard/{p}": c for p, c in rendered.files.items()}
        lab_network._run(HAPROXY_IMAGE, EDGE, ["sh", "-c", EDGE_START], files, user="root")
        yield lab_network


def _req(label: str, port: int, path: str, *args: str, host: str = "app.example.test") -> str:
    return f"req {label} {resolve(host, port)} {' '.join(args)} https://{host}:{port}{path}"


def _hdrs(label: str, port: int, path: str, *args: str, host: str = "app.example.test") -> str:
    return f"hdrs {label} {resolve(host, port)} {' '.join(args)} https://{host}:{port}{path}"


@pytest.fixture(scope="module")
def match_results(lab: Lab) -> dict[str, dict[str, str]]:
    def run(client: str) -> dict[str, str]:
        script = "\n".join(
            [
                _req("get", 443, "/"),
                _req("post", 443, "/", "-X POST"),
                _req("canary", 443, "/", "-H 'X-Canary: yes'"),
                _req("canary-no", 443, "/", "-H 'X-Canary: no'"),
                _req("query", 443, "/?canary=1"),
                _req("query-no", 443, "/?canary=2"),
                _req("sni", 443, "/s/x", host="sni.example.test"),
                _req("sni-path", 443, "/other", host="sni.example.test"),
                _req("hdr", 443, "/h", "-H 'X-Pre: abcd'"),
                _req("hdr-gone", 443, "/h", "-H 'X-Pre: abcd' -H 'X-Gone: 1'"),
                _req("hdr-pre", 443, "/h", "-H 'X-Pre: zabcd'"),
            ]
        )
        return lab.curl(client, script)

    return {"98": run("192.0.2.98"), "99": run("192.0.2.99")}


class TestMatchModel:
    def test_source_network_routes_only_that_client(
        self, match_results: dict[str, dict[str, str]]
    ) -> None:
        assert match_results["98"]["get"].startswith("be=C ")
        assert match_results["99"]["get"].startswith("be=A ")

    def test_method_header_and_query_conditions(
        self, match_results: dict[str, dict[str, str]]
    ) -> None:
        got = match_results["99"]
        assert got["post"].startswith("be=B ")
        assert got["canary"].startswith("be=B ")
        assert got["canary-no"].startswith("be=A ")
        assert got["query"].startswith("be=B ")
        assert got["query-no"].startswith("be=A ")

    def test_sni_and_path_must_both_hold(self, match_results: dict[str, dict[str, str]]) -> None:
        got = match_results["99"]
        assert got["sni"].startswith("be=C ")
        assert got["sni-path"].startswith("be=A ")

    def test_header_prefix_and_absence(self, match_results: dict[str, dict[str, str]]) -> None:
        got = match_results["99"]
        assert got["hdr"].startswith("be=B ")
        assert got["hdr-gone"].startswith("be=A "), "X-Gone is present, so the route must not match"
        assert got["hdr-pre"].startswith("be=A "), "prefix is anchored at the start"


@pytest.fixture(scope="module")
def rewrite_results(lab: Lab) -> dict[str, str]:
    script = "\n".join(
        [
            _req("plain", 8443, "/x", "-H 'Cookie: s=1'", "-H 'X-Edge: spoofed'"),
            _req("internal", 8443, "/internal/x", "-H 'Cookie: s=1'"),
            _req("strip", 8443, "/api/v1/users?q=1"),
            _req("strip-root", 8443, "/api/v1"),
            _req("strip-other", 8443, "/api/v10/users"),
            _req("legacy", 8443, "/legacy/users"),
            _req("legacy-root", 8443, "/legacy"),
            _req("legacy-other", 8443, "/legacyx"),
            _req("via", 8443, "/", "-H 'X-Trace: 1'"),
            _req("via-post", 8443, "/", "-H 'X-Trace: 1'", "-X POST"),
            _hdrs("resp", 8443, "/"),
            _hdrs("static", 8443, "/static/a.css"),
        ]
    )
    return lab.curl("192.0.2.99", HDRS + script)


class TestRewrite:
    def test_headers_are_set_and_client_supplied_copies_replaced(
        self, rewrite_results: dict[str, str]
    ) -> None:
        out = rewrite_results["plain"]
        assert " edge=switchyard " in out
        assert " cip=192.0.2.99 " in out

    def test_header_removal_is_conditional(self, rewrite_results: dict[str, str]) -> None:
        assert " cookie=s=1 " in rewrite_results["plain"]
        assert " cookie= " in rewrite_results["internal"]

    def test_prefix_is_stripped_on_segment_boundaries(
        self, rewrite_results: dict[str, str]
    ) -> None:
        assert " path=/users query=q=1 " in rewrite_results["strip"]
        assert " path=/ " in rewrite_results["strip-root"]
        assert " path=/api/v10/users " in rewrite_results["strip-other"]

    def test_prefix_is_replaced_on_segment_boundaries(
        self, rewrite_results: dict[str, str]
    ) -> None:
        assert " path=/v2/users " in rewrite_results["legacy"]
        assert " path=/v2 " in rewrite_results["legacy-root"]
        assert " path=/legacyx " in rewrite_results["legacy-other"]

    def test_conditions_combine(self, rewrite_results: dict[str, str]) -> None:
        assert " via=1.1 sy " in rewrite_results["via"]
        assert " via= " in rewrite_results["via-post"]

    def test_response_headers(self, rewrite_results: dict[str, str]) -> None:
        headers = rewrite_results["resp"].lower().split("~")
        assert "strict-transport-security: max-age=60" in headers
        assert "x-origin: a" in headers
        assert not [h for h in headers if h.startswith("server:")], "Server must be removed"
        assert not [h for h in headers if h.startswith("cache-control:")]

    def test_response_rule_uses_the_request_match(self, rewrite_results: dict[str, str]) -> None:
        assert "cache-control: public" in rewrite_results["static"].lower().split("~")


@pytest.fixture(scope="module")
def respond_results(lab: Lab) -> dict[str, str]:
    script = "\n".join(
        [
            _req("ping", 9443, "/lb-ping"),
            _hdrs("ping-h", 9443, "/lb-ping"),
            _req("wp", 9443, "/wp-admin/x"),
            _req("fav", 9443, "/favicon.ico"),
            _req("maint", 9443, "/ok", "-H 'X-Maint: on'"),
            _hdrs("maint-h", 9443, "/ok", "-H 'X-Maint: on'"),
            _req("ok", 9443, "/ok"),
            _req("dead", 9443, "/other"),
            _hdrs("dead-h", 9443, "/other"),
        ]
    )
    return lab.curl("192.0.2.99", HDRS + script)


class TestFixedResponses:
    def test_body_status_and_type(self, respond_results: dict[str, str]) -> None:
        assert respond_results["ping"] == "pong"
        assert respond_results["ping-h"].startswith("HTTP/1.1 200")
        assert "content-type: text/plain" in respond_results["ping-h"].lower().split("~")

    def test_status_without_body(self, respond_results: dict[str, str]) -> None:
        assert respond_results["wp"] == "|404|"
        assert respond_results["fav"] == "|204|"

    def test_response_wins_over_routing_and_carries_retry_after(
        self, respond_results: dict[str, str]
    ) -> None:
        assert respond_results["maint"] == "<h1>maintenance</h1>"
        assert respond_results["maint-h"].startswith("HTTP/1.1 503")
        assert "retry-after: 600" not in respond_results["maint-h"]
        assert "retry-after: 30" in respond_results["maint-h"].lower().split("~")

    def test_unmatched_requests_still_route(self, respond_results: dict[str, str]) -> None:
        assert respond_results["ok"].startswith("be=A ")

    def test_error_page_replaces_the_generated_503(self, respond_results: dict[str, str]) -> None:
        assert respond_results["dead"] == "<h1>custom 503</h1>"
        assert respond_results["dead-h"].startswith("HTTP/1.1 503")
