"""Real requests through the rendered match model, rewrite policies and fixed responses.

The golden cases prove syntax (``haproxy -c``); this proves what the rules do to traffic. Same lab
as ``test_haproxy_behavior``: 192.0.2.20 edge, backends 192.0.2.51-53 that echo what they
received, clients 192.0.2.98 and 192.0.2.99.
"""

import time
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

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
    http-request return status 200 content-type text/plain lf-string "be={label} method=%[method] path=%[path] query=%[query] edge=%[req.hdr(x-edge)] cip=%[req.hdr(x-client-ip)] cookie=%[req.hdr(cookie)] via=%[req.hdr(via)] host=%[req.hdr(host)] au=%[req.hdr(x-auth-user)] ae=%[req.hdr(x-auth-email)] ag=%[req.fhdr(x-auth-groups)] at=%[req.hdr(x-auth-token)] fu=%[req.hdr(x-forwarded-user)] sa=%[req.hdr(x-sy-access)] az=%[req.hdr(authorization)] xff=%[req.hdr(x-forwarded-for)] proto=%[req.hdr(x-forwarded-proto)]" hdr Server backend-secret hdr X-Origin {label}
"""

#: Nothing listens on the edge's port 8080, so a pool pointing at it makes HAProxy answer 503.
DEAD = EDGE

#: Records the response headers of a request as one line (``~`` between header lines).
HDRS = r"""
hdrs() { label=$1; shift; out=$(curl -s -k --max-time 6 -D - -o /dev/null "$@" | tr -d '\r' | tr '\n' '~'); echo "$label: $out"; }
"""


#: Stands in for oauth2-proxy on the engine: no cookie means the login page or a redirect to it, a
#: cookie ``sy=<user>:<email>:<groups separated by |>:<id token>`` is a session and the request goes on to the loopback bind
#: with the headers the real proxy sets.
STUB_PROXY = """global
    maxconn 100
defaults
    mode http
    timeout client 5s
    timeout server 5s
    timeout connect 5s
frontend p
    bind 127.0.0.1:14000
    http-request return status 200 content-type text/plain string login-page if { path_beg /oauth2/ }
    http-request return status 302 hdr Location /oauth2/start unless { req.cook(sy) -m found }
    http-request set-header X-Forwarded-User %[req.cook(sy),field(1,:)]
    http-request set-header X-Forwarded-Email %[req.cook(sy),field(2,:)]
    http-request set-header X-Forwarded-Groups "%[req.cook(sy),field(3,:),regsub('[|]',',',g)]"
    http-request set-header Authorization "Bearer %[req.cook(sy),field(4,:)]"
    default_backend up
backend up
    server s 127.0.0.1:18000
"""

EDGE_WITH_PROXY = "haproxy -D -f /stub.cfg" + chr(10) + EDGE_START


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
                "id": "lim",
                "tenant_id": "t",
                "name": "lim",
                "kind": "rate_limit",
                "rules": [
                    {
                        "match": {"path_exact": "/login", "methods": ["POST"]},
                        "max_requests": 3,
                        "period_s": 60,
                    },
                    {
                        "key": "header",
                        "header_name": "X-Api-Key",
                        "max_requests": 2,
                        "period_s": 60,
                    },
                ],
            },
            {
                "id": "sso",
                "tenant_id": "t",
                "name": "sso",
                "kind": "access",
                "oidc": {
                    "issuer": "https://login.example.test",
                    "client_id": "lab",
                    "client_secret_ref": "kv/tenants/acme/auth/c",
                    "cookie_secret_ref": "kv/tenants/acme/auth/k",
                    "cookie_name": "sy",
                },
                "rules": [
                    {"match": {"path_prefix": "/health"}, "mode": "public"},
                    {
                        "match": {"path_prefix": "/posture"},
                        "mode": "login",
                        "posture": {
                            "claims": [
                                {"name": "email_verified", "op": "true"},
                                {"name": "email_verified", "op": "equals", "value": "true"},
                                {"name": "tier", "op": "in", "values": ["gold", "silver"]},
                                {"name": "mfa", "op": "present"},
                            ]
                        },
                    },
                    {"match": {"path_prefix": "/admin"}, "mode": "groups", "groups": ["ops.team"]},
                    {
                        "match": {"path_prefix": "/staff"},
                        "mode": "email_domains",
                        "email_domains": ["example.test"],
                    },
                ],
                "identity_headers": {"token": "X-Auth-Token"},
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
            service("limits", 7443, policy_set_ids=["lim"]),
            service("login", 6443, policy_set_ids=["sso"]),
            service("zlogin", 6444, policy_set_ids=["sso"]),
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
        files["stub.cfg"] = STUB_PROXY
        lab_network._run(HAPROXY_IMAGE, EDGE, ["sh", "-c", EDGE_WITH_PROXY], files, user="root")
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


@pytest.fixture(scope="module")
def limit_results(lab: Lab) -> dict[str, dict[str, str]]:
    def run(client: str) -> dict[str, str]:
        lines = [_hdrs(f"login{i}", 7443, "/login", "-X POST") for i in range(1, 6)]
        lines += [_hdrs("get", 7443, "/login")]
        lines += [_hdrs(f"key-a{i}", 7443, "/x", "-H 'X-Api-Key: a'") for i in range(1, 4)]
        lines += [_hdrs("key-b", 7443, "/x", "-H 'X-Api-Key: b'")]
        lines += [_hdrs(f"nokey{i}", 7443, "/x") for i in range(1, 5)]
        return lab.curl(client, HDRS + chr(10).join(lines))

    return {"99": run("192.0.2.99"), "98": run("192.0.2.98")}


def _status(line: str) -> str:
    return line.split("~", 1)[0]


class TestRateLimits:
    def test_path_limit_denies_after_the_allowance_with_a_retry_hint(
        self, limit_results: dict[str, dict[str, str]]
    ) -> None:
        got = limit_results["99"]
        assert [_status(got[f"login{i}"]) for i in range(1, 6)] == [
            "HTTP/1.1 200 OK",
            "HTTP/1.1 200 OK",
            "HTTP/1.1 200 OK",
            "HTTP/1.1 429 Too Many Requests",
            "HTTP/1.1 429 Too Many Requests",
        ]
        assert "retry-after: 60" in got["login4"].lower().split("~")

    def test_requests_outside_the_match_are_not_counted(
        self, limit_results: dict[str, dict[str, str]]
    ) -> None:
        assert _status(limit_results["99"]["get"]) == "HTTP/1.1 200 OK"

    def test_counters_are_per_client(self, limit_results: dict[str, dict[str, str]]) -> None:
        assert _status(limit_results["98"]["login1"]) == "HTTP/1.1 200 OK"

    def test_header_key_counts_per_value_and_ignores_requests_without_it(
        self, limit_results: dict[str, dict[str, str]]
    ) -> None:
        got = limit_results["99"]
        assert [_status(got[f"key-a{i}"]) for i in range(1, 4)] == [
            "HTTP/1.1 200 OK",
            "HTTP/1.1 200 OK",
            "HTTP/1.1 429 Too Many Requests",
        ]
        assert _status(got["key-b"]) == "HTTP/1.1 200 OK"
        assert all(_status(got[f"nokey{i}"]) == "HTTP/1.1 200 OK" for i in range(1, 5))


_KEY = RSAKey.generate_key(2048)


def id_token(**claims: Any) -> str:
    """A signed ID token like the provider issues (the engine trusts the proxy that verified it)."""
    body = {"iss": "https://login.example.test", "sub": "alice", "exp": int(time.time()) + 600}
    return jwt.encode({"alg": "RS256"}, body | claims, _KEY)


ID_TOKEN = id_token(email_verified=True, tier="gold", mfa=["otp"])


def cookie(token: str) -> str:
    return f"-H 'Cookie: sy=alice:alice@example.test:staff:{token}'"


SESSION = f"-H 'Cookie: sy=alice:alice@example.test:staff|ops.team:{ID_TOKEN}'"


@pytest.fixture(scope="module")
def login_results(lab: Lab) -> dict[str, str]:
    script = chr(10).join(
        [
            _req("health", 6443, "/health", "-H 'X-Auth-User: root' -H 'X-Sy-Access: 2'"),
            _req("anon", 6443, "/private"),
            _req("start", 6443, "/oauth2/start"),
            _req("user", 6443, "/private", SESSION, "-H 'X-Auth-User: root'"),
            _req("admin", 6443, "/admin", SESSION),
            _req("admin-no", 6443, "/admin", "-H 'Cookie: sy=bob:bob@example.test:staff:x'"),
            _req("staff", 6443, "/staff", SESSION),
            _req("posture", 6443, "/posture", SESSION),
            _req(
                "posture-false",
                6443,
                "/posture",
                cookie(id_token(email_verified=False, tier="gold", mfa=[1])),
            ),
            _req(
                "posture-tier",
                6443,
                "/posture",
                cookie(id_token(email_verified=True, tier="bronze", mfa=[1])),
            ),
            _req(
                "posture-nomfa",
                6443,
                "/posture",
                cookie(id_token(email_verified=True, tier="gold")),
            ),
            _req(
                "cookies",
                6443,
                "/private",
                f"-H 'Cookie: other=1; sy=alice:alice@example.test:staff:{ID_TOKEN}; sy_csrf=abc; sy_1=zz; keep=2'",
            ),
            _req("staff-no", 6443, "/staff", "-H 'Cookie: sy=eve:eve@evil.test:ops.team:x'"),
            _hdrs("down-h", 6444, "/private", SESSION),
            _req("down-public", 6444, "/health"),
        ]
    )
    return lab.curl("192.0.2.99", HDRS + script)


class TestAccess:
    def test_public_rule_skips_the_login_and_client_identity_headers_are_dropped(
        self, login_results: dict[str, str]
    ) -> None:
        out = login_results["health"]
        assert out.startswith("be=A ") and out.endswith("|200|")
        assert " au= " in out and " sa= " in out, (
            "client-supplied identity must not reach the origin"
        )

    def test_anonymous_request_is_sent_to_the_login_and_never_reaches_the_origin(
        self, login_results: dict[str, str]
    ) -> None:
        assert login_results["anon"] == "|302|https://app.example.test:6443/oauth2/start"
        assert login_results["start"] == "login-page|200|"

    def test_signed_in_user_reaches_the_origin_with_identity_headers(
        self, login_results: dict[str, str]
    ) -> None:
        out = login_results["user"]
        assert out.startswith("be=A ")
        for part in (
            " au=alice ",
            " ae=alice@example.test ",
            " ag=staff,ops.team ",
            f" at={ID_TOKEN} ",
        ):
            assert part in out
        assert " fu= " in out and " sa= " in out and " az= " in out, "proxy headers must not leak"

    def test_the_real_client_address_and_scheme_survive_the_loopback_hop(
        self, login_results: dict[str, str]
    ) -> None:
        out = login_results["user"]
        assert " xff=192.0.2.99 " in out and " proto=https" in out

    def test_group_and_domain_rules(self, login_results: dict[str, str]) -> None:
        assert login_results["admin"].startswith("be=A ")
        assert login_results["admin-no"] == "|403|"
        assert login_results["staff"].startswith("be=A ")
        assert login_results["staff-no"] == "|403|"

    def test_a_dead_proxy_closes_protected_paths_but_not_public_ones(
        self, login_results: dict[str, str]
    ) -> None:
        assert login_results["down-h"].startswith("HTTP/1.1 503")
        assert login_results["down-public"].startswith("be=A ")

    def test_posture_claims_match_real_json_booleans_and_arrays(
        self, login_results: dict[str, str]
    ) -> None:
        assert login_results["posture"].startswith("be=A ")
        assert login_results["posture-false"] == "|403|"
        assert login_results["posture-tier"] == "|403|"
        assert login_results["posture-nomfa"] == "|403|"

    def test_the_proxys_cookies_never_reach_the_origin(self, login_results: dict[str, str]) -> None:
        out = login_results["cookies"]
        assert out.startswith("be=A ")
        assert " cookie=other=1; keep=2 " in out or " cookie=other=1;keep=2 " in out, out
