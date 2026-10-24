# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""JSON body schemas on an API operation: the stored tree and its change-set limits (ADR 0228).

The schema an operation carries is itself untrusted input, so these tests are about what the
change set refuses: depth, property count, names, and the two settings a body schema needs to be
enforced at all (``inspect_body`` and, for blocking, the WAF's own block mode).
"""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from switchyard.domain import models as dm
from switchyard.domain.loader import snapshot_from_mapping
from tests.factories import BASE


def waf(**extra: Any) -> dict[str, Any]:
    return {"id": "ps-waf", "tenant_id": "t-acme", "name": "waf", "kind": "waf", **extra}


def policy(*, waf_mode: str = "detect", inspect_body: bool = True, **api: Any) -> dict[str, Any]:
    """A WAF policy carrying ``api``, attached to the base service's HTTP virtual service."""
    d = copy.deepcopy(BASE)
    d["policy_sets"] = [waf(mode=waf_mode, inspect_body=inspect_body, api=api)]
    d["virtual_services"][0]["policy_set_ids"] = ["ps-waf"]
    return d


def body(**extra: Any) -> dict[str, Any]:
    return {
        "content_types": ["application/json"],
        "body": {"properties": [{"name": "sku"}]},
        **extra,
    }


def operation(**extra: Any) -> dict[str, Any]:
    return {"method": "POST", "path": "/pets", **body(**extra)}


def refuses(*, waf_mode: str = "detect", inspect_body: bool = True, **api: Any) -> None:
    with pytest.raises(ValidationError):
        snapshot_from_mapping(policy(waf_mode=waf_mode, inspect_body=inspect_body, **api))


def test_a_body_schema_is_parsed_into_the_operation() -> None:
    snap = snapshot_from_mapping(policy(operations=[operation()]))
    when = snap.policy_sets[0]
    assert isinstance(when, dm.WafPolicySet)
    assert when.api is not None
    stored = when.api.operations[0].body
    assert stored is not None
    assert stored.properties[0].name == "sku"
    assert stored.additional_properties is False


def test_a_property_name_the_renderer_cannot_embed_is_refused() -> None:
    for name in ("with space", "dash-ed", "1leading", "a" * 65, "é"):
        refuses(
            operations=[
                {
                    "method": "POST",
                    "path": "/pets",
                    "content_types": ["application/json"],
                    "body": {"properties": [{"name": name}]},
                }
            ]
        )


def test_a_deep_schema_is_refused_at_the_change_set() -> None:
    def nested(depth: int) -> dict[str, Any]:
        node: dict[str, Any] = {"name": "leaf"}
        for level in range(depth):
            node = {"name": f"l{level}", "kind": "object", "properties": [node]}
        return {"properties": [node]}

    snapshot_from_mapping(
        policy(
            operations=[
                {
                    "method": "POST",
                    "path": "/pets",
                    "content_types": ["application/json"],
                    "body": nested(dm.API_BODY_MAX_DEPTH - 1),
                }
            ]
        )
    )
    refuses(
        operations=[
            {
                "method": "POST",
                "path": "/pets",
                "content_types": ["application/json"],
                "body": nested(dm.API_BODY_MAX_DEPTH),
            }
        ]
    )


def test_too_many_properties_are_refused() -> None:
    many = {"properties": [{"name": f"p{i}"} for i in range(dm.API_BODY_MAX_PROPERTIES + 1)]}
    refuses(
        operations=[
            {"method": "POST", "path": "/pets", "content_types": ["application/json"], "body": many}
        ]
    )


def test_too_many_body_operations_are_refused() -> None:
    operations = [
        {"method": "POST", "path": f"/p{i}", **body()}
        for i in range(dm.API_BODY_MAX_OPERATIONS + 1)
    ]
    refuses(operations=operations)


def test_a_duplicate_member_name_in_one_object_is_refused() -> None:
    refuses(
        operations=[
            {
                "method": "POST",
                "path": "/pets",
                "content_types": ["application/json"],
                "body": {"properties": [{"name": "a"}, {"name": "a"}]},
            }
        ]
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "a", "kind": "array"},  # items missing
        {"name": "a", "items": "string"},  # items without array
        {"name": "a", "kind": "object"},  # properties missing
        {"name": "a", "kind": "enum"},  # values missing
        {"name": "a", "values": ["x"]},  # values without enum
        {"name": "a", "max_items": 3},  # max_items without array
        {"name": "a", "max_length": 0},
        {"name": "a", "max_length": 4097},
        {"name": "a", "kind": "array", "items": "object"},
        {"name": "a", "kind": "array", "items": "string", "max_items": 1001},
        {"name": "a", "kind": "enum", "values": ["x", "x"]},
    ],
)
def test_a_malformed_property_is_a_field_error(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as caught:
        dm.ApiBodyProperty.model_validate(bad)
    assert caught.value.errors()


def test_a_body_needs_a_json_content_type() -> None:
    refuses(
        operations=[
            {
                "method": "POST",
                "path": "/pets",
                "content_types": ["text/plain"],
                "body": {"properties": [{"name": "sku"}]},
            }
        ]
    )


def test_a_body_needs_inspect_body_or_the_sidecar_never_sees_it() -> None:
    refuses(inspect_body=False, operations=[operation()])


def test_blocking_a_body_needs_the_waf_in_block_mode() -> None:
    refuses(waf_mode="detect", mode="block", operations=[operation()])
    snapshot_from_mapping(policy(waf_mode="block", mode="block", operations=[operation()]))
    snapshot_from_mapping(policy(waf_mode="detect", mode="detect", operations=[operation()]))
