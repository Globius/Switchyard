"""Match model, rewrite, respond and error-page policy sets: model rules and snapshot checks."""

from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain.loader import snapshot_from_mapping
from switchyard.domain.models import (
    ErrorPagesPolicySet,
    HeaderMatch,
    QueryMatch,
    RespondPolicySet,
    ResponseRule,
    RewritePolicySet,
    RewriteRule,
    RouteMatch,
)
from switchyard.domain.snapshot import validate_snapshot
from switchyard.domain.validators import validate_body, validate_header_value, validate_query_param
from tests.factories import base_data

T = "t-acme"
TENANT = "22222222-2222-2222-2222-222222222222"


class TestRouteMatch:
    def test_empty_and_host_only(self) -> None:
        assert RouteMatch().is_empty
        assert not RouteMatch(host="a.example.test").is_empty
        assert RouteMatch(host="a.example.test").is_host_only
        assert not RouteMatch(path_prefix="/x").is_host_only

    @pytest.mark.parametrize(
        "extra",
        [
            {"methods": ["GET"]},
            {"headers": [{"name": "X-A", "op": "present"}]},
            {"query": [{"name": "a", "op": "present"}]},
            {"source_cidrs": ["10.0.0.0/8"]},
            {"sni": "a.example.test"},
        ],
    )
    def test_any_condition_makes_the_match_non_empty_and_not_host_only(
        self, extra: dict[str, Any]
    ) -> None:
        assert not RouteMatch(**extra).is_empty
        assert not RouteMatch(host="a.example.test", **extra).is_host_only

    def test_path_prefix_and_exact_are_exclusive(self) -> None:
        with pytest.raises(ValidationError, match="not both"):
            RouteMatch(path_prefix="/a", path_exact="/a")

    def test_methods_must_be_known_and_unique(self) -> None:
        with pytest.raises(ValidationError):
            RouteMatch(methods=["TRACE"])
        with pytest.raises(ValidationError, match="unique"):
            RouteMatch(methods=["GET", "GET"])

    def test_wildcard_sni_has_one_label(self) -> None:
        assert RouteMatch(sni="*.example.test").sni == "*.example.test"
        with pytest.raises(ValidationError):
            RouteMatch(sni="a.*.example.test")

    def test_source_cidrs_are_networks(self) -> None:
        match = RouteMatch(source_cidrs=["10.0.0.0/8", "fd00::/8"])
        assert [str(n) for n in match.source_cidrs] == ["10.0.0.0/8", "fd00::/8"]
        with pytest.raises(ValidationError):
            RouteMatch(source_cidrs=["10.0.0.1/8"])  # host bits set

    def test_condition_counts_are_capped(self) -> None:
        with pytest.raises(ValidationError):
            RouteMatch(source_cidrs=[f"10.0.{i}.0/24" for i in range(17)])
        with pytest.raises(ValidationError):
            RouteMatch(headers=[{"name": f"X-{i}", "op": "present"} for i in range(9)])


class TestHeaderAndQueryMatch:
    @pytest.mark.parametrize("op", ["equals", "prefix", "suffix", "contains"])
    def test_comparisons_need_a_value(self, op: str) -> None:
        with pytest.raises(ValidationError, match="needs a value"):
            HeaderMatch(name="X-A", op=op)
        assert HeaderMatch(name="X-A", op=op, value="v").value == "v"

    @pytest.mark.parametrize("op", ["present", "absent"])
    def test_presence_takes_no_value(self, op: str) -> None:
        with pytest.raises(ValidationError, match="takes no value"):
            HeaderMatch(name="X-A", op=op, value="v")

    def test_query_ops(self) -> None:
        assert QueryMatch(name="a", op="present").value is None
        assert QueryMatch(name="a", value="1").op == "equals"
        with pytest.raises(ValidationError, match="needs a value"):
            QueryMatch(name="a", op="equals")
        with pytest.raises(ValidationError, match="takes no value"):
            QueryMatch(name="a", op="present", value="1")

    @pytest.mark.parametrize("name", ["a b", "a=b", "a&b", "a\nb", "", "x" * 65, "a'b", "a$b"])
    def test_query_names_are_strict(self, name: str) -> None:
        with pytest.raises(ValueError, match="query parameter"):
            validate_query_param(name)

    @pytest.mark.parametrize("name", ["utm_source", "a.b-c", "A1"])
    def test_query_names_accept_plain_names(self, name: str) -> None:
        assert validate_query_param(name) == name


class TestHeaderValue:
    @pytest.mark.parametrize(
        "value",
        ["canary", "max-age=31536000; includeSubDomains", "public, max-age=60", "1.1 proxy", "*"],
    )
    def test_accepts_plain_values(self, value: str) -> None:
        assert validate_header_value(value) == value

    @pytest.mark.parametrize(
        "value",
        [
            "",
            " lead",
            "trail ",
            'a"b',
            "a'b\"c",
            "a\\b",
            "a$b",
            "${PATH}",
            "%[src]",
            "a#b",
            "a\nb",
            "a\tb",
            "a\x00b",
            "x" * 257,
        ],
    )
    def test_rejects_anything_the_config_parser_treats_specially(self, value: str) -> None:
        with pytest.raises(ValueError, match="header value"):
            validate_header_value(value)


class TestRewriteRule:
    @staticmethod
    def rule(**kw: Any) -> RewriteRule:
        return RewriteRule.model_validate(kw)

    def test_header_action_needs_exactly_one_of_value_and_variable(self) -> None:
        action = {"type": "set_header", "name": "X-A"}
        with pytest.raises(ValidationError, match="exactly one"):
            self.rule(action=action)
        with pytest.raises(ValidationError, match="exactly one"):
            self.rule(action={**action, "value": "v", "variable": "client_ip"})
        assert self.rule(action={**action, "variable": "client_ip"}).phase == "request"

    def test_variables_come_from_a_fixed_list(self) -> None:
        with pytest.raises(ValidationError):
            self.rule(action={"type": "set_header", "name": "X-A", "variable": "src,lower"})

    @pytest.mark.parametrize(
        "name",
        ["Content-Length", "transfer-encoding", "Connection", "Upgrade", "Proxy-Authorization"],
    )
    @pytest.mark.parametrize("phase", ["request", "response"])
    def test_framing_and_hop_by_hop_headers_are_off_limits(self, name: str, phase: str) -> None:
        with pytest.raises(ValidationError, match="cannot be changed"):
            self.rule(phase=phase, action={"type": "remove_header", "name": name})

    @pytest.mark.parametrize("name", ["X-Forwarded-For", "x-forwarded-proto"])
    def test_client_address_headers_stay_with_the_renderer(self, name: str) -> None:
        with pytest.raises(ValidationError, match="managed by Switchyard"):
            self.rule(action={"type": "set_header", "name": name, "value": "x"})
        # a response header of the same name is the origin's business
        assert self.rule(
            phase="response", action={"type": "set_header", "name": name, "value": "x"}
        )

    def test_host_can_only_be_set(self) -> None:
        assert self.rule(action={"type": "set_header", "name": "Host", "value": "a.test"})
        with pytest.raises(ValidationError, match="only be set"):
            self.rule(action={"type": "remove_header", "name": "Host"})
        with pytest.raises(ValidationError, match="only be set"):
            self.rule(action={"type": "add_header", "name": "host", "value": "a.test"})

    def test_replace_prefix_is_request_only(self) -> None:
        action = {"type": "replace_prefix", "from_prefix": "/a", "to_prefix": "/b"}
        assert self.rule(action=action)
        with pytest.raises(ValidationError, match="request phase"):
            self.rule(phase="response", action=action)

    @pytest.mark.parametrize(
        ("source", "target"), [("/", "/b"), ("/a/", "/b"), ("/a", "/b/"), ("a", "/b")]
    )
    def test_replace_prefix_shapes(self, source: str, target: str) -> None:
        with pytest.raises(ValidationError):
            self.rule(action={"type": "replace_prefix", "from_prefix": source, "to_prefix": target})

    def test_replace_prefix_may_strip(self) -> None:
        assert self.rule(action={"type": "replace_prefix", "from_prefix": "/a", "to_prefix": "/"})

    def test_a_set_needs_rules_and_is_capped(self) -> None:
        base = {"id": "11111111-1111-1111-1111-111111111111", "tenant_id": TENANT, "name": "rw"}
        one = {"action": {"type": "remove_header", "name": "X-A"}}
        assert RewritePolicySet.model_validate({**base, "rules": [one]}).kind == "rewrite"
        with pytest.raises(ValidationError):
            RewritePolicySet.model_validate({**base, "rules": []})
        with pytest.raises(ValidationError):
            RewritePolicySet.model_validate({**base, "rules": [one] * 65})


class TestResponses:
    def test_body_and_content_type_go_together(self) -> None:
        with pytest.raises(ValidationError, match="go together"):
            ResponseRule(status=200, body="x")
        with pytest.raises(ValidationError, match="go together"):
            ResponseRule(status=200, content_type="text/plain")
        assert ResponseRule(status=200, content_type="text/plain", body="ok").body == "ok"
        assert ResponseRule(status=404).body is None

    def test_an_empty_body_is_not_a_body(self) -> None:
        with pytest.raises(ValidationError):
            ResponseRule(status=200, content_type="text/plain", body="")

    def test_204_has_no_body(self) -> None:
        with pytest.raises(ValidationError, match="204"):
            ResponseRule(status=204, content_type="text/plain", body="x")
        assert ResponseRule(status=204).status == 204

    def test_retry_after_only_with_429_or_503(self) -> None:
        assert ResponseRule(status=503, retry_after_s=30).retry_after_s == 30
        assert ResponseRule(status=429, retry_after_s=1)
        with pytest.raises(ValidationError, match="429 or 503"):
            ResponseRule(status=404, retry_after_s=30)
        with pytest.raises(ValidationError):
            ResponseRule(status=503, retry_after_s=0)

    def test_only_listed_statuses_and_types(self) -> None:
        with pytest.raises(ValidationError):
            ResponseRule(status=301)  # redirects have their own rules
        with pytest.raises(ValidationError):
            ResponseRule(status=200, content_type="application/x-evil", body="x")

    def test_an_empty_match_answers_everything(self) -> None:
        assert ResponseRule(status=503).match.is_empty

    def test_body_limits(self) -> None:
        assert validate_body("line1\r\nline2\ttab") == "line1\r\nline2\ttab"
        assert validate_body("é" * 8000)  # 16000 bytes
        with pytest.raises(ValueError, match="16 KiB"):
            validate_body("é" * 8200)
        for bad in ("a\x00b", "a\x1bb", "a\x7fb"):
            with pytest.raises(ValueError, match="control characters"):
                validate_body(bad)

    def test_error_pages_are_one_per_status(self) -> None:
        base = {"id": "11111111-1111-1111-1111-111111111111", "tenant_id": TENANT, "name": "e"}
        page = {"status": 503, "body": "x"}
        assert ErrorPagesPolicySet.model_validate({**base, "pages": [page]}).pages[0].content_type
        with pytest.raises(ValidationError, match="one page per status"):
            ErrorPagesPolicySet.model_validate({**base, "pages": [page, page]})
        with pytest.raises(ValidationError):
            ErrorPagesPolicySet.model_validate({**base, "pages": [{"status": 404, "body": "x"}]})

    def test_respond_set_kind(self) -> None:
        base = {"id": "11111111-1111-1111-1111-111111111111", "tenant_id": TENANT, "name": "r"}
        assert (
            RespondPolicySet.model_validate({**base, "rules": [{"status": 404}]}).kind == "respond"
        )


def with_policies(*policies: dict[str, Any], attach: list[str] | None = None) -> dict[str, Any]:
    data = base_data()
    data["policy_sets"] = list(policies)
    data["virtual_services"][0]["policy_set_ids"] = attach or [p["id"] for p in policies]
    return data


def codes(data: dict[str, Any]) -> list[str]:
    return [p.code for p in validate_snapshot(snapshot_from_mapping(data))]


RW = {
    "id": "ps-rw",
    "tenant_id": T,
    "name": "rw",
    "kind": "rewrite",
    "rules": [{"action": {"type": "set_header", "name": "X-A", "value": "1"}}],
}
RESP = {
    "id": "ps-resp",
    "tenant_id": T,
    "name": "resp",
    "kind": "respond",
    "rules": [{"status": 404}],
}
ERR = {
    "id": "ps-err",
    "tenant_id": T,
    "name": "err",
    "kind": "error_pages",
    "pages": [{"status": 503, "body": "down"}],
}
ALLOW = {
    "id": "ps-allow",
    "tenant_id": T,
    "name": "allow",
    "kind": "allow_list",
    "cidrs": ["10.0.0.0/8"],
}


class TestSnapshotRules:
    def test_every_attachable_kind_is_accepted(self) -> None:
        assert codes(with_policies(RW, RESP, ERR)) == []

    def test_an_allow_list_cannot_be_attached_as_a_policy(self) -> None:
        assert codes(with_policies(ALLOW)) == ["wrong_policy_kind"]

    def test_a_rewrite_set_cannot_be_the_allow_list(self) -> None:
        data = with_policies(RW, attach=[])
        data["virtual_services"][0]["allow_list_id"] = "ps-rw"
        assert codes(data) == ["wrong_policy_kind"]

    def test_two_error_page_sets_may_not_define_the_same_status(self) -> None:
        other = {**ERR, "id": "ps-err2", "name": "err2"}
        assert codes(with_policies(ERR, other)) == ["duplicate_error_page"]
        distinct = {**other, "pages": [{"status": 502, "body": "bad"}]}
        assert codes(with_policies(ERR, distinct)) == []

    def test_tcp_service_cannot_attach_policies(self) -> None:
        data = with_policies(RW)
        vs = data["virtual_services"][0]
        vs.update(protocol="tcp", routes=[], default_pool_id="pool-web", ports=[{"port": 443}])
        vs.pop("certificate_ids")
        with pytest.raises(ValidationError, match="cannot have routes or policy sets"):
            snapshot_from_mapping(data)

    def test_routes_differing_only_in_extra_conditions_are_not_duplicates(self) -> None:
        data = base_data()
        host = {"host": "app.example.test"}
        data["virtual_services"][0]["routes"] = [
            {"match": {**host, "methods": ["GET"]}, "pool_id": "pool-web"},
            {"match": {**host, "methods": ["POST"]}, "pool_id": "pool-web"},
        ]
        assert codes(data) == []
        data["virtual_services"][0]["routes"].append(
            {"match": {**host, "methods": ["GET"]}, "pool_id": "pool-web"}
        )
        assert codes(data) == ["duplicate_route"]

    def test_a_route_may_match_on_a_method_alone(self) -> None:
        data = base_data()
        data["virtual_services"][0]["routes"] = [
            {"match": {"methods": ["POST"]}, "pool_id": "pool-web"}
        ]
        assert codes(data) == []
