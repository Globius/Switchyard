"""API schema (ADR 0197): models, the regular expressions and the rules that use them."""

import re
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from switchyard.domain import models as dm
from switchyard.render import apischema as aps
from switchyard.render.escape import RenderError


def op(method: str = "GET", path: str = "/pets", **kw: Any) -> dm.ApiOperation:
    return dm.ApiOperation(method=method, path=path, **kw)


def schema(*ops: dm.ApiOperation, **kw: Any) -> dm.ApiSchema:
    return dm.ApiSchema(operations=ops or (op(),), **kw)


class TestModels:
    @pytest.mark.parametrize(
        "path",
        [
            "pets",
            "/",
            "/a b",
            "/a//b",
            "/pets/{}",
            "/pets/{1x}",
            "/pets/{a b}",
            "/x/" + "y/" * 16,
            "/a?b",
        ],
    )
    def test_bad_path_templates(self, path: str) -> None:
        with pytest.raises(ValidationError):
            op(path=path)

    def test_path_params_must_exist_in_the_path(self) -> None:
        with pytest.raises(ValidationError, match="path_params"):
            op(path="/pets/{id}", path_params=[{"name": "other"}])
        with pytest.raises(ValidationError, match="twice"):
            op(path="/a/{id}/{id}")
        with pytest.raises(ValidationError, match="always required"):
            op(path="/a/{id}", path_params=[{"name": "id", "required": True}])

    def test_enum_values_go_with_enum_only(self) -> None:
        with pytest.raises(ValidationError):
            dm.ApiParam(name="s", kind="enum")
        with pytest.raises(ValidationError):
            dm.ApiParam(name="s", kind="string", values=("a",))
        with pytest.raises(ValidationError, match="unique"):
            dm.ApiParam(name="s", kind="enum", values=("a", "a"))

    def test_query_and_content_types_are_unique(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            op(query=[{"name": "a"}, {"name": "a"}])
        with pytest.raises(ValidationError, match="unique"):
            op(content_types=["application/json", "application/json"])

    def test_the_same_method_and_template_cannot_be_listed_twice(self) -> None:
        with pytest.raises(ValidationError, match="twice"):
            schema(op(path="/p/{a}"), op(path="/p/{b}"))
        schema(op(path="/p/{a}"), op("POST", "/p/{a}"))

    def test_limits(self) -> None:
        with pytest.raises(ValidationError):
            dm.ApiSchema(operations=())
        with pytest.raises(ValidationError):
            schema(base_path="v1")
        with pytest.raises(ValidationError):
            schema(base_path="/a/b/c/d/e")
        with pytest.raises(ValidationError):
            op(max_body_bytes=-1)

    def test_a_waf_policy_may_carry_a_schema(self) -> None:
        policy = dm.WafPolicySet.model_validate(
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "tenant_id": "00000000-0000-0000-0000-000000000002",
                "name": "w",
                "api": {"operations": [{"method": "GET", "path": "/ping"}]},
            }
        )
        assert policy.api is not None and policy.api.mode == "detect"


class TestRegex:
    def test_path_regex_matches_only_the_template(self) -> None:
        o = op(
            path="/pets/{id}/photos.json",
            path_params=[dm.ApiParam(name="id", kind="integer")],
        )
        rx = re.compile(aps.path_regex(schema(o, base_path="/v1"), o))
        assert rx.fullmatch("/v1/pets/12/photos.json")
        assert rx.fullmatch("/v1/pets/-3/photos.json")
        for bad in (
            "/v1/pets/x/photos.json",
            "/v1/pets/12/photosXjson",  # the dot is literal
            "/pets/12/photos.json",
            "/v1/pets/12/photos.json/extra",
            "/v1/pets/1/2/photos.json",
        ):
            assert not rx.fullmatch(bad), bad

    @pytest.mark.parametrize(
        ("kind", "good", "bad"),
        [
            ("integer", ["0", "-5", "123456789012345678"], ["", "1.5", "a", "1234567890123456789"]),
            ("number", ["1", "-1.25", "0.5"], ["", ".5", "1.", "1e5"]),
            ("boolean", ["true", "false"], ["True", "1", ""]),
            (
                "uuid",
                ["123e4567-e89b-12d3-a456-426614174000"],
                ["123e4567", "g23e4567-e89b-12d3-a456-426614174000"],
            ),
        ],
    )
    def test_value_kinds(self, kind: str, good: list[str], bad: list[str]) -> None:
        rx = re.compile(aps.value_regex(dm.ApiParam(name="v", kind=kind), in_path=False))
        assert all(rx.fullmatch(v) for v in good)
        assert not any(rx.fullmatch(v) for v in bad)

    def test_string_length_and_path_slash(self) -> None:
        p = dm.ApiParam(name="s", max_length=5)
        assert re.fullmatch(aps.value_regex(p, in_path=False), "abcde")
        assert not re.fullmatch(aps.value_regex(p, in_path=False), "abcdef")
        assert not re.fullmatch(aps.value_regex(p, in_path=False), "a&b")
        assert not re.fullmatch(aps.value_regex(p, in_path=True), "a/b")

    def test_query_regex_checks_every_pair(self) -> None:
        o = op(
            query=[
                dm.ApiParam(name="limit", kind="integer"),
                dm.ApiParam(name="q"),
                dm.ApiParam(name="v.1", kind="boolean"),
            ]
        )
        rx = re.compile(aps.query_regex(o))
        for good in ("limit=5", "q", "q=", "q=hello%20x&limit=2", "limit=1&limit=2", "v.1=true"):
            assert rx.fullmatch(good), good
        for bad in (
            "limit=x",
            "limit=1&limit=x",  # a repeated parameter is checked too
            "evil=1",
            "limit=1&evil=1",
            "limit",
            "q=a&&limit=1",
            "vX1=true",  # the dot is literal
            "limit=1&",
        ):
            assert not rx.fullmatch(bad), bad

    @given(st.text(alphabet=st.characters(min_codepoint=33, max_codepoint=126), max_size=40))
    def test_an_integer_regex_never_matches_what_int_rejects(self, text: str) -> None:
        rx = re.compile(aps.value_regex(dm.ApiParam(name="n", kind="integer"), in_path=False))
        if rx.fullmatch(text):
            assert int(text) is not None

    def test_unsafe_names_are_refused_when_they_reach_the_renderer(self) -> None:
        bad = dm.ApiParam.model_construct(name="a'b", kind="string", values=(), max_length=5)
        with pytest.raises(RenderError):
            aps.query_regex(op(query=[bad]))


class TestRules:
    def lines(self, *ops: dm.ApiOperation, **kw: Any) -> list[str]:
        return aps.request_lines(schema(*ops, **kw))

    def test_literal_paths_are_tried_before_parameters(self) -> None:
        lines = self.lines(op(path="/orders/{ref}"), op(path="/orders/mine"))
        sets = [ln for ln in lines if "set-var(txn.sy_api) int(" in ln]
        assert "int(1)" in sets[0] and "orders/mine" in sets[0]
        assert "int(2)" in sets[1]

    def test_first_violation_wins_and_is_captured(self) -> None:
        lines = self.lines(op(query=[dm.ApiParam(name="a", required=True)]))
        violations = [ln for ln in lines if "set-var(txn.sy_apiv)" in ln]
        assert violations and all("!{ var(txn.sy_apiv) -m found }" in ln for ln in violations)
        assert "http-request capture var(txn.sy_apiv) len 8" in lines

    def test_detect_mode_never_denies(self) -> None:
        text = "\n".join(self.lines(op(), mode="detect"))
        assert "deny" not in text and "return status" not in text

    def test_block_mode_maps_reasons_to_statuses(self) -> None:
        text = "\n".join(self.lines(op(), mode="block"))
        for reason, status in (("path", 404), ("method", 405), ("ctype", 415), ("size", 413)):
            assert f"status {status}" in text and reason in text
        assert "str query param" in text and "deny_status 400" in text

    def test_dot_segments_and_encoded_separators_are_refused(self) -> None:
        text = "\n".join(self.lines(op()))
        assert "(^|/)[.]{1,2}(/|$)" in text and "%2[eEfF]|%5[cC]" in text

    def test_an_operation_without_query_refuses_any_query_unless_allowed(self) -> None:
        strict = "\n".join(self.lines(op()))
        assert "{ query -m reg '.' }" in strict
        loose = "\n".join(self.lines(op(), allow_unknown_query=True))
        assert "{ query -m reg '.' }" not in loose

    def test_allow_unknown_query_still_types_the_known_parameters(self) -> None:
        text = "\n".join(
            self.lines(
                op(query=[dm.ApiParam(name="n", kind="integer", required=True)]),
                allow_unknown_query=True,
            )
        )
        assert "url_param(n) -m found" in text and "url_param(n) -m reg '^-?[0-9]{1,18}$'" in text

    def test_body_rules(self) -> None:
        text = "\n".join(
            self.lines(
                op("POST", "/u", content_types=["application/json", "text/csv"], max_body_bytes=10),
                max_body_bytes=99,
            )
        )
        assert "gt 10" in text and "gt 99" not in text  # the operation's own limit wins
        assert 'lower -m str "application/json" "text/csv"' in text
        assert "transfer-encoding) -m found" in text

    def test_patterns_are_single_quoted_and_free_of_spaces(self) -> None:
        for line in self.lines(op(path="/a/{x}"), op("POST", "/a")):
            for pattern in re.findall(r"-m reg (\S+)", line):
                assert pattern.startswith("'") and pattern.endswith("'")
