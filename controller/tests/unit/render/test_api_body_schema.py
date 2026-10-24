# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Generated body schemas and their Coraza rules (ADR 0228).

The documents are checked with a JSON Schema validator (``jsonschema``, draft 2020-12), which is
what Coraza's ``@validateSchema`` operator does with ``kaptinlin/jsonschema``: a valid body passes,
a wrong type, a missing required member and an unknown member are violations. No container is
needed for that, and none is used here.
"""

import copy
import json
from typing import Any

import jsonschema
import pytest

from switchyard.domain import models as dm
from switchyard.domain.loader import snapshot_from_mapping
from switchyard.render import apischema
from switchyard.render.waf import directives, render_waf_files
from tests.factories import BASE

PET = {
    "name": "rex",
    "kind": "cat",
    "owner": {"email": "a@example.test"},
    "tags": ["friendly"],
}


def body(**extra: Any) -> dict[str, Any]:
    return {
        "properties": [
            {"name": "name", "kind": "string", "required": True, "max_length": 8},
            {"name": "age", "kind": "integer"},
            {"name": "kind", "kind": "enum", "values": ["cat", "dog"], "required": True},
            {"name": "chip", "kind": "uuid"},
            {
                "name": "owner",
                "kind": "object",
                "required": True,
                "properties": [{"name": "email", "kind": "string", "required": True}],
            },
            {"name": "tags", "kind": "array", "items": "string", "max_items": 2},
        ],
        **extra,
    }


def operation(**extra: Any) -> dict[str, Any]:
    return {
        "method": "POST",
        "path": "/pets",
        "content_types": ["application/json"],
        "body": body(),
        **extra,
    }


def snapshot(*operations: dict[str, Any], mode: str = "block", waf_mode: str = "block") -> Any:
    d = copy.deepcopy(BASE)
    d["policy_sets"] = [
        {
            "id": "ps-waf",
            "tenant_id": "t-acme",
            "name": "waf",
            "kind": "waf",
            "mode": waf_mode,
            "api": {"mode": mode, "base_path": "/v1", "operations": list(operations)},
        }
    ]
    d["virtual_services"][0]["policy_set_ids"] = ["ps-waf"]
    return snapshot_from_mapping(d)


def rendered(*operations: dict[str, Any], mode: str = "block", waf_mode: str = "block") -> Any:
    snap = snapshot(*operations, mode=mode, waf_mode=waf_mode)
    return snap, render_waf_files(snap, snap.se_groups[0].id)


def policy_of(snap: Any) -> dm.WafPolicySet:
    policy = snap.policy_sets[0]
    assert isinstance(policy, dm.WafPolicySet)
    return policy


def document(snap: Any, number: int = 1) -> Any:
    return json.loads(
        render_waf_files(snap, snap.se_groups[0].id)[apischema.body_file_name("web", number)]
    )


def test_a_valid_body_passes_the_generated_schema() -> None:
    snap, _ = rendered(operation())
    jsonschema.Draft202012Validator(document(snap)).validate(PET)


def test_a_wrong_type_is_blocked() -> None:
    snap, _ = rendered(operation())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(document(snap)).validate({**PET, "age": "three"})


def test_a_missing_required_property_is_blocked() -> None:
    snap, _ = rendered(operation())
    for missing in ("name", "kind", "owner"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(document(snap)).validate(
                {k: v for k, v in PET.items() if k != missing}
            )


@pytest.mark.parametrize(
    "bad",
    [
        {**PET, "unknown": 1},
        {**PET, "kind": "cow"},
        {**PET, "name": "x" * 9},
        {**PET, "chip": "not-a-uuid"},
        {**PET, "owner": {"email": 5}},
        {**PET, "owner": {"email": "a@example.test", "phone": "1"}},
        {**PET, "tags": ["a", "b", "c"]},
        {**PET, "tags": [1]},
    ],
)
def test_a_body_that_leaves_the_documented_shape_is_blocked(bad: dict[str, Any]) -> None:
    snap, _ = rendered(operation())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(document(snap)).validate(bad)


def test_the_schema_is_the_documented_mapping_of_the_stored_tree() -> None:
    snap, _ = rendered(operation())
    api = policy_of(snap).api
    assert api is not None
    stored = api.operations[0].body
    assert stored is not None
    doc = document(snap)
    assert doc["additionalProperties"] is False
    assert doc["required"] == ["name", "kind", "owner"]
    assert doc["properties"]["age"] == {"type": "integer"}
    assert doc["properties"]["chip"]["pattern"].startswith("[0-9A-Fa-f]{8}-")
    assert doc["properties"]["tags"] == {
        "type": "array",
        "items": {"type": "string", "maxLength": 256},
        "maxItems": 2,
    }


def test_the_rules_land_in_the_same_coraza_config_as_the_crs() -> None:
    snap, files = rendered(operation())
    config = files["coraza/config.yaml"]
    assert config.index("Include /usr/share/coreruleset/4.8.0/rules/*.conf") < config.index(
        "@validateSchema"
    )
    lines = directives("web", policy_of(snap))
    assert lines[1] == "SecRuleEngine On"
    assert lines[-3].startswith('SecRule REQUEST_METHOD "@streq POST" "id:100001,')
    assert lines[-2] == 'SecRule REQUEST_FILENAME "@rx ^/v1/pets$" "chain,t:none"'
    assert lines[-1] == (
        'SecRule REQUEST_BODY "@validateSchema /etc/switchyard/coraza/schemas/web-1.json" "t:none"'
    )
    assert "deny,status:400" in lines[-3]


def test_detect_mode_logs_the_violation_and_blocks_nothing() -> None:
    snap, _ = rendered(operation(), mode="detect", waf_mode="detect")
    lines = directives("web", policy_of(snap))
    assert "pass" in lines[-3]
    assert "deny" not in "".join(lines)


def test_every_schema_a_rule_names_is_in_the_file_set() -> None:
    snap, files = rendered(operation(), operation(path="/pets/{id}", method="PUT"))
    named = [
        line.split("@validateSchema ")[1].split('"')[0]
        for line in directives("web", policy_of(snap))
        if "@validateSchema" in line
    ]
    assert len(named) == 2
    for path in named:
        relative = path.removeprefix("/etc/switchyard/")
        assert relative in files
    assert sorted(files) == [
        "coraza/config.yaml",
        "coraza/schemas/web-1.json",
        "coraza/schemas/web-2.json",
    ]


def test_rule_ids_follow_the_operation_number_and_stay_under_the_crs_range() -> None:
    snap, _ = rendered(operation(), operation(path="/pets/{id}", method="PUT"))
    ids = [
        int(line.split('"id:')[1].split(",")[0])
        for line in directives("web", policy_of(snap))
        if line.startswith("SecRule REQUEST_METHOD") and '"id:' in line
    ]
    assert ids == [apischema.BODY_RULE_ID_BASE + 1, apischema.BODY_RULE_ID_BASE + 2]
    assert all(rule_id < 900_000 for rule_id in ids)


def test_an_operation_without_a_body_renders_no_rule_and_no_file() -> None:
    plain = {"method": "GET", "path": "/pets"}
    _, files = rendered(plain)
    assert "@validateSchema" not in files["coraza/config.yaml"]
    assert list(files) == ["coraza/config.yaml"]


def test_the_generated_rule_text_and_schemas_are_deterministic() -> None:
    """Same state, same bytes: twice through the renderer, and from a second parsing of it."""

    def from_mapping() -> Any:
        return snapshot(operation(), operation(method="PUT", path="/pets/{id}"))

    snap = from_mapping()
    group = snap.se_groups[0].id
    first = render_waf_files(snap, group)
    assert render_waf_files(snap, group) == first
    again = from_mapping()
    assert render_waf_files(again, again.se_groups[0].id) == first
    assert directives("web", policy_of(snap)) == directives("web", policy_of(again))


def test_a_body_file_carries_a_trailing_newline_and_no_absolute_reference() -> None:
    _, files = rendered(operation())
    text = files["coraza/schemas/web-1.json"]
    assert text.endswith("}\n")
    assert "$ref" not in text and "http" not in text
