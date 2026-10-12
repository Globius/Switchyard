"""Renderer behaviour for the match model, rewrite, respond and error-page policy sets."""

import uuid
from typing import Any

import pytest

from switchyard.domain.changes import ObjectType
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.domain.models import (
    ErrorPage,
    HeaderMatch,
    HeaderOp,
    QueryMatch,
    QueryOp,
    ReplacePrefix,
    ResponseRule,
    RewritePolicySet,
    RewriteRule,
    RouteMatch,
    SetHeader,
)
from switchyard.domain.snapshot import Snapshot
from switchyard.render.escape import RenderError
from switchyard.render.fragments import rendered_fragments
from switchyard.render.haproxy import render_haproxy
from tests.factories import base_data

GROUP = uuid.UUID(symbolic_uuid("seg-1"))
T = "t-acme"


def render(data: dict[str, Any]) -> dict[str, str]:
    return dict(render_haproxy(snapshot_from_mapping(data), GROUP).files)


def frontend(data: dict[str, Any]) -> list[str]:
    """The indented lines of the first frontend, stripped."""
    text = render(data)["haproxy/haproxy.cfg"]
    block = text.split("frontend vs_web\n", 1)[1].split("\n\n", 1)[0]
    return [line.strip() for line in block.splitlines()]


def rewrite(*rules: dict[str, Any], pid: str = "ps-rw", name: str = "rw") -> dict[str, Any]:
    return {"id": pid, "tenant_id": T, "name": name, "kind": "rewrite", "rules": list(rules)}


def respond(*rules: dict[str, Any], pid: str = "ps-resp", name: str = "resp") -> dict[str, Any]:
    return {"id": pid, "tenant_id": T, "name": name, "kind": "respond", "rules": list(rules)}


def attach(data: dict[str, Any], *policies: dict[str, Any]) -> dict[str, Any]:
    data["policy_sets"] = list(policies)
    data["virtual_services"][0]["policy_set_ids"] = [p["id"] for p in policies]
    return data


def set_header(name: str, value: str, **extra: Any) -> dict[str, Any]:
    return {"action": {"type": "set_header", "name": name, "value": value}, **extra}


class TestRouting:
    def routes(self, *routes: dict[str, Any]) -> list[str]:
        data = base_data()
        data["virtual_services"][0]["routes"] = list(routes)
        return frontend(data)

    def test_host_with_another_condition_is_a_conditional_route_not_a_map_entry(self) -> None:
        data = base_data()
        data["virtual_services"][0]["routes"] = [
            {"match": {"host": "app.example.test", "methods": ["POST"]}, "pool_id": "pool-web"},
            {"match": {"host": "app.example.test"}, "pool_id": "pool-web"},
        ]
        files = render(data)
        lines = frontend(data)
        assert (
            "use_backend pool_web if { var(txn.host) -m str app.example.test } { method POST }"
            in lines
        )
        assert files["haproxy/maps/web-host.map"] == "app.example.test pool_web\n"

    def test_conditional_routes_keep_their_order_across_kinds_of_condition(self) -> None:
        lines = self.routes(
            {
                "order": 2,
                "match": {"query": [{"name": "a", "op": "present"}]},
                "pool_id": "pool-web",
            },
            {"order": 1, "match": {"path_prefix": "/p"}, "pool_id": "pool-web"},
            {"order": 3, "match": {"methods": ["GET"]}, "pool_id": "pool-web"},
        )
        routing = [line for line in lines if line.startswith("use_backend")]
        assert routing == [
            "use_backend pool_web if { path_beg /p }",
            "use_backend pool_web if { url_param(a) -m found }",
            "use_backend pool_web if { method GET }",
        ]

    def test_conditions_render_for_every_kind(self) -> None:
        match = RouteMatch(
            host="app.example.test",
            path_exact="/x",
            methods=["PUT", "GET"],
            headers=[
                HeaderMatch(name="X-E", op="equals", value="a b"),
                HeaderMatch(name="X-B", op="prefix", value="p"),
                HeaderMatch(name="X-S", op="suffix", value="s"),
                HeaderMatch(name="X-C", op="contains", value="c"),
                HeaderMatch(name="X-P", op="present"),
                HeaderMatch(name="X-A", op="absent"),
            ],
            query=[QueryMatch(name="q", op="present"), QueryMatch(name="r", value="1")],
            source_cidrs=["fd00::/8", "10.0.0.0/8", "192.0.2.0/24"],
            sni="*.example.test",
        )
        from switchyard.render.haproxy import _Renderer

        assert _Renderer._conditions(match) == [
            "{ var(txn.host) -m str app.example.test }",
            "{ path /x }",
            "{ method GET PUT }",
            '{ req.hdr(X-E) -m str "a b" }',
            '{ req.hdr(X-B) -m beg "p" }',
            '{ req.hdr(X-S) -m end "s" }',
            '{ req.hdr(X-C) -m sub "c" }',
            "{ req.hdr(X-P) -m found }",
            "!{ req.hdr(X-A) -m found }",
            "{ url_param(q) -m found }",
            '{ url_param(r) -m str "1" }',
            "{ src 10.0.0.0/8 192.0.2.0/24 fd00::/8 }",
            "{ ssl_fc_sni -m reg '^[^.]+[.]example[.]test$' }",
        ]

    def test_exact_sni(self) -> None:
        lines = self.routes({"match": {"sni": "app.example.test"}, "pool_id": "pool-web"})
        assert "use_backend pool_web if { ssl_fc_sni -m str app.example.test }" in lines

    def test_redirect_rule_with_extra_conditions(self) -> None:
        data = attach(
            base_data(),
            {
                "id": "ps-r",
                "tenant_id": T,
                "name": "r",
                "kind": "redirect",
                "rules": [
                    {
                        "match": {"methods": ["GET"], "query": [{"name": "d", "op": "present"}]},
                        "redirect": {"location": "/", "code": 302},
                    }
                ],
            },
        )
        assert (
            "http-request redirect location / code 302 if { method GET } { url_param(d) -m found }"
            in frontend(data)
        )


class TestStages:
    def test_redirects_then_responses_then_rewrites_whatever_the_attachment_order(self) -> None:
        redirect = {
            "id": "ps-red",
            "tenant_id": T,
            "name": "red",
            "kind": "redirect",
            "rules": [{"match": {"path_exact": "/old"}, "redirect": {"location": "/new"}}],
        }
        data = attach(
            base_data(),
            rewrite(set_header("X-A", "1")),
            respond({"match": {"path_exact": "/gone"}, "status": 410}),
            redirect,
        )
        data["virtual_services"][0]["routes"].append(
            {"match": {"path_exact": "/r"}, "redirect": {"location": "/s"}}
        )
        rules = [line for line in frontend(data) if line.startswith("http-request")]
        kinds = [r.split()[1] for r in rules]
        assert kinds[-5:] == ["redirect", "redirect", "redirect", "return", "set-header"]
        # policy-set redirects come before redirect routes (unchanged since ADR 0006)
        assert "/old" in rules[-4]
        assert "/r " in rules[-3]
        # and everything runs after header hygiene and before routing
        text = "\n".join(frontend(data))
        assert text.index("set-var(txn.host)") < text.index("http-request return")
        assert text.index("http-request set-header X-A") < text.index("use_backend")

    def test_sets_of_one_kind_apply_in_attachment_order(self) -> None:
        data = attach(
            base_data(),
            rewrite(set_header("X-Second", "2"), pid="ps-2", name="second"),
            rewrite(set_header("X-First", "1"), pid="ps-1", name="first"),
        )
        headers = [line for line in frontend(data) if "X-First" in line or "X-Second" in line]
        assert [h.split()[2] for h in headers] == ["X-Second", "X-First"]

    def test_no_policy_sets_means_unchanged_output(self) -> None:
        lines = frontend(base_data())
        assert not any(line.startswith(("http-response", "http-error")) for line in lines)
        assert not any(" return " in line for line in lines)


class TestRewrite:
    def lines(self, *rules: dict[str, Any]) -> list[str]:
        return [
            line
            for line in frontend(attach(base_data(), rewrite(*rules)))
            if line.startswith(("http-request set", "http-request add", "http-request del"))
            or line.startswith(("http-request replace", "http-response"))
        ][4:]  # skip the hygiene rules the renderer always writes

    def test_actions_and_phases(self) -> None:
        got = self.lines(
            set_header("X-A", "v w"),
            {"action": {"type": "add_header", "name": "X-B", "variable": "tls_sni"}},
            {"action": {"type": "remove_header", "name": "Cookie"}},
            set_header("X-R", "1", phase="response"),
            {"phase": "response", "action": {"type": "remove_header", "name": "Server"}},
        )
        assert got == [
            'http-request set-header X-A "v w"',
            'http-request add-header X-B "%[ssl_fc_sni]"',
            "http-request del-header Cookie",
            'http-response set-header X-R "1"',
            "http-response del-header Server",
        ]

    @pytest.mark.parametrize(
        ("variable", "fetch"),
        [
            ("client_ip", "src"),
            ("client_port", "src_port"),
            ("host", "var(txn.host)"),
            ("vip", "dst"),
            ("vip_port", "dst_port"),
            ("method", "method"),
            ("tls_version", "ssl_fc_protocol"),
            ("tls_cipher", "ssl_fc_cipher"),
            ("tls_sni", "ssl_fc_sni"),
            ("request_id", "uuid"),
        ],
    )
    def test_variables_map_to_the_fixed_fetches(self, variable: str, fetch: str) -> None:
        got = self.lines({"action": {"type": "set_header", "name": "X-V", "variable": variable}})
        assert got == [f'http-request set-header X-V "%[{fetch}]"']

    def test_response_rule_with_a_match_uses_a_transaction_flag(self) -> None:
        got = self.lines(
            set_header("X-One", "1", phase="response", match={"path_prefix": "/a"}),
            set_header("X-Two", "2", phase="response", match={"methods": ["GET"]}),
            set_header("X-All", "3", phase="response"),
        )
        assert got == [
            "http-request set-var(txn.sy_m1) bool(true) if { path_beg /a }",
            "http-request set-var(txn.sy_m2) bool(true) if { method GET }",
            'http-response set-header X-One "1" if { var(txn.sy_m1) -m bool }',
            'http-response set-header X-Two "2" if { var(txn.sy_m2) -m bool }',
            'http-response set-header X-All "3"',
        ]

    def test_replace_prefix(self) -> None:
        got = self.lines(
            {
                "action": {
                    "type": "replace_prefix",
                    "from_prefix": "/api/v1.2",
                    "to_prefix": "/v2+x",
                },
                "match": {"methods": ["GET"]},
            },
        )
        assert got == [
            "http-request replace-path '^/api/v1[.]2(/.*)?$' '/v2+x\\1' if { method GET }"
        ]

    def test_strip_prefix_handles_the_bare_prefix_first(self) -> None:
        got = self.lines(
            {"action": {"type": "replace_prefix", "from_prefix": "/api", "to_prefix": "/"}}
        )
        assert got == [
            "http-request set-path / if { path /api }",
            "http-request replace-path '^/api(/.*)$' '\\1'",
        ]

    def test_percent_in_a_replacement_is_literal(self) -> None:
        got = self.lines(
            {"action": {"type": "replace_prefix", "from_prefix": "/a%20b", "to_prefix": "/c%20d"}}
        )
        assert got == ["http-request replace-path '^/a%20b(/.*)?$' '/c%%20d\\1'"]


class TestResponses:
    def lines(self, data: dict[str, Any]) -> list[str]:
        return [
            line
            for line in frontend(data)
            if line.startswith(("http-request return", "http-error"))
        ]

    def test_rules_files_and_conditions(self) -> None:
        data = attach(
            base_data(),
            respond(
                {
                    "match": {"path_exact": "/ping"},
                    "status": 200,
                    "content_type": "text/plain",
                    "body": "pong",
                },
                {"status": 503, "retry_after_s": 30},
                {
                    "match": {"methods": ["POST"]},
                    "status": 429,
                    "content_type": "application/json",
                    "body": '{"a":1}',
                    "retry_after_s": 5,
                },
            ),
        )
        assert self.lines(data) == [
            "http-request return status 200 content-type text/plain "
            "file /etc/switchyard/haproxy/lists/resp-r1.page if { path /ping }",
            "http-request return status 503 hdr Retry-After 30",
            "http-request return status 429 content-type application/json "
            "file /etc/switchyard/haproxy/lists/resp-r3.page hdr Retry-After 5 if { method POST }",
        ]
        files = render(data)
        assert files["haproxy/lists/resp-r1.page"] == "pong\n"
        assert files["haproxy/lists/resp-r3.page"] == '{"a":1}\n'
        assert "haproxy/lists/resp-r2.page" not in files

    def test_error_pages(self) -> None:
        data = attach(
            base_data(),
            {
                "id": "ps-e",
                "tenant_id": T,
                "name": "errs",
                "kind": "error_pages",
                "pages": [
                    {"status": 504, "content_type": "text/plain", "body": "slow"},
                    {"status": 503, "body": "<p>down</p>\n"},
                ],
            },
        )
        assert self.lines(data) == [
            "http-error status 503 content-type text/html "
            "file /etc/switchyard/haproxy/lists/errs-e503.page",
            "http-error status 504 content-type text/plain "
            "file /etc/switchyard/haproxy/lists/errs-e504.page",
        ]
        files = render(data)
        assert files["haproxy/lists/errs-e503.page"] == "<p>down</p>\n"
        assert files["haproxy/lists/errs-e504.page"] == "slow\n"

    def test_fragments_of_a_policy_set_include_its_pages(self) -> None:
        data = attach(
            base_data(),
            respond({"status": 200, "content_type": "text/plain", "body": "x"}),
        )
        snap = snapshot_from_mapping(data)
        found = rendered_fragments(snap, ObjectType.POLICY_SET, uuid.UUID(symbolic_uuid("ps-resp")))
        assert sorted(f.file for f in found) == [
            "haproxy/haproxy.cfg",
            "haproxy/lists/resp-r1.page",
        ]


class TestHostileValues:
    """Values that skip the models (``model_construct``) must not reach the config."""

    def attach_built(self, **built: Any) -> Snapshot:
        """Render a snapshot whose first policy rule was built without validation."""
        snap = snapshot_from_mapping(attach(base_data(), rewrite(set_header("X-A", "1"))))
        policy = snap.policy_sets[0]
        assert isinstance(policy, RewritePolicySet)
        rule = policy.rules[0].model_copy(update=built)
        evil_policy = policy.model_copy(update={"rules": (rule,)})
        return snap.model_copy(update={"policy_sets": (evil_policy,)})

    @pytest.mark.parametrize(
        "bad", ['a"b', "a\nb", "a$b", "a\\b", "%[src]", "a#b", "${PATH}", "a\tb", "a\x00b"]
    )
    def test_header_values(self, bad: str) -> None:
        action = SetHeader.model_construct(type="set_header", name="X-A", value=bad, variable=None)
        evil = self.attach_built(action=action)
        with pytest.raises(RenderError):
            render_haproxy(evil, GROUP)

    @pytest.mark.parametrize("bad", ["X A", "X-A\nhttp-request deny", "X:A", 'X"A', "X#A"])
    def test_header_names(self, bad: str) -> None:
        action = SetHeader.model_construct(type="set_header", name=bad, value="1", variable=None)
        evil = self.attach_built(action=action)
        with pytest.raises(RenderError):
            render_haproxy(evil, GROUP)

    @pytest.mark.parametrize("bad", ["/a b", "/a'b", "/a\\b", "/a$b", "/a\nb", "/a#b", "a"])
    @pytest.mark.parametrize("field", ["from_prefix", "to_prefix"])
    def test_prefixes(self, bad: str, field: str) -> None:
        fields = {"from_prefix": "/a", "to_prefix": "/b", field: bad}
        action = ReplacePrefix.model_construct(
            from_prefix=fields["from_prefix"], to_prefix=fields["to_prefix"]
        )
        evil = self.attach_built(action=action)
        with pytest.raises(RenderError):
            render_haproxy(evil, GROUP)

    @pytest.mark.parametrize("bad", ['a"b', "a\nb", "a$b", "a\\b", "a#b"])
    def test_match_operands(self, bad: str) -> None:
        for match in (
            RouteMatch.model_construct(
                host=None,
                path_prefix=None,
                path_exact=None,
                methods=(),
                headers=(HeaderMatch.model_construct(name="X-A", op=HeaderOp.EQUALS, value=bad),),
                query=(),
                source_cidrs=(),
                sni=None,
            ),
            RouteMatch.model_construct(
                host=None,
                path_prefix=None,
                path_exact=None,
                methods=(),
                headers=(),
                query=(QueryMatch.model_construct(name="q", op=QueryOp.EQUALS, value=bad),),
                source_cidrs=(),
                sni=None,
            ),
            RouteMatch.model_construct(
                host=None,
                path_prefix=None,
                path_exact=None,
                methods=(),
                headers=(),
                query=(QueryMatch.model_construct(name=bad, op=QueryOp.PRESENT, value=None),),
                source_cidrs=(),
                sni=None,
            ),
            RouteMatch.model_construct(
                host=None,
                path_prefix=None,
                path_exact=None,
                methods=(),
                headers=(),
                query=(),
                source_cidrs=(),
                sni=bad,
            ),
        ):
            evil = self.attach_built(match=match)
            with pytest.raises(RenderError):
                render_haproxy(evil, GROUP)

    def test_response_rule_fields(self) -> None:
        snap = snapshot_from_mapping(attach(base_data(), respond({"status": 404})))
        policy = snap.policy_sets[0]
        # a status outside the allowed set never gets this far (model), but a body is a file
        # name's neighbour, so it is checked only as data; the file name comes from validated names
        rule = ResponseRule.model_construct(
            match=RouteMatch(),
            status=200,
            content_type="text/plain",
            body="x\x00y",
            retry_after_s=None,
        )
        evil = snap.model_copy(
            update={"policy_sets": (policy.model_copy(update={"rules": (rule,)}),)}
        )
        # the body is written to a file, never into the config; the config stays well-formed
        files = dict(render_haproxy(evil, GROUP).files)
        assert "x\x00y" not in files["haproxy/haproxy.cfg"]

    def test_page_names_come_from_validated_policy_names(self) -> None:
        snap = snapshot_from_mapping(attach(base_data(), respond({"status": 404})))
        policy = snap.policy_sets[0].model_copy(update={"name": "a b"})
        rule = ResponseRule.model_construct(
            match=RouteMatch(), status=200, content_type="text/plain", body="x", retry_after_s=None
        )
        evil = snap.model_copy(
            update={"policy_sets": (policy.model_copy(update={"rules": (rule,)}),)}
        )
        with pytest.raises(RenderError):
            render_haproxy(evil, GROUP)

    def test_error_page_name(self) -> None:
        data = attach(
            base_data(),
            {
                "id": "ps-e",
                "tenant_id": T,
                "name": "errs",
                "kind": "error_pages",
                "pages": [{"status": 503, "body": "x"}],
            },
        )
        snap = snapshot_from_mapping(data)
        policy = snap.policy_sets[0].model_copy(update={"name": "e;x"})
        evil = snap.model_copy(update={"policy_sets": (policy,)})
        with pytest.raises(RenderError):
            render_haproxy(evil, GROUP)
        assert ErrorPage(status=503, body="x").content_type == "text/html"
        assert RewriteRule(action=SetHeader(name="X-A", value="1")).phase == "request"
