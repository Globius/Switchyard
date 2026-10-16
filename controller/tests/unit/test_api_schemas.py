# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The OpenAPI document types every response and documents every route (T2, T4)."""

from typing import Any

import pytest
from fastapi.routing import APIRoute
from pydantic import TypeAdapter

from switchyard.api.app import COLLECTIONS, create_app
from switchyard.api.examples import CREATE_BODIES, OBJECTS
from switchyard.api.schemas import OBJECT_APIS

APP = create_app(None)  # type: ignore[arg-type]
SPEC: dict[str, Any] = APP.openapi()
SCHEMAS: dict[str, Any] = SPEC["components"]["schemas"]
METHODS = {"get", "post", "put", "patch", "delete"}

#: Free-form by nature: a partial object, a per-job-kind result, a diff, a quota map.
FREE_FORM = {
    "ChangeOpRead.body",
    "OpIn.body",
    "JobRead.result",
    "Job.result",
    "AuditEventRead.diff",
    "IdpTestResult.claims",
    "AlertTargetRead.config",  # per kind: {url}, {url, format} or {to}
    "AlertRuleRead.spec",  # per kind: actions, a metric threshold or days
    "AlertFiring.details",  # the numbers behind one alert
}


def _api_routes() -> list[APIRoute]:
    """The routes as declared (FastAPI wraps included routers; ``original_router`` has them)."""
    return [
        r
        for wrapper in APP.routes
        if hasattr(wrapper, "original_router")
        for r in wrapper.original_router.routes
        if isinstance(r, APIRoute)
    ]


def _resolve(node: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in node:
        node = SCHEMAS[node["$ref"].split("/")[-1]]
    return node


def _untyped(node: dict[str, Any], where: str, seen: set[str]) -> list[str]:
    """``Model.field`` of every property that is an open-ended object or says nothing at all."""
    if "$ref" in node:
        name = node["$ref"].split("/")[-1]
        if name in seen:
            return []
        seen.add(name)
        return _untyped(SCHEMAS[name], name, seen)
    found: list[str] = []
    for branch in node.get("anyOf", []) + node.get("oneOf", []) + node.get("allOf", []):
        found += _untyped(branch, where, seen)
    if "items" in node:
        found += _untyped(node["items"], where, seen)
    for key, prop in node.get("properties", {}).items():
        label = f"{where}.{key}"
        typed = any(k in prop for k in ("type", "$ref", "anyOf", "oneOf", "allOf", "enum", "const"))
        open_object = any(
            "additionalProperties" in b and b["additionalProperties"] is True
            for b in [prop, *prop.get("anyOf", [])]
        )
        if not typed or open_object:
            found.append(label)
        else:
            found += _untyped(prop, label, seen)
    return found


def _operations() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method.upper(), path, op)
        for path, item in SPEC["paths"].items()
        for method, op in item.items()
        if method in METHODS
    ]


def _json_2xx(op: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for code, response in op["responses"].items():
        schema = response.get("content", {}).get("application/json", {}).get("schema")
        if code.startswith("2") and schema is not None:
            out.append((code, schema))
    return out


def test_every_operation_has_a_summary() -> None:
    missing = [f"{m} {p}" for m, p, op in _operations() if not op.get("summary")]
    assert missing == []


def test_every_2xx_body_is_typed() -> None:
    offenders: set[str] = set()
    for method, path, op in _operations():
        for code, schema in _json_2xx(op):
            if not any(k in schema for k in ("$ref", "anyOf", "oneOf", "items", "properties")):
                offenders.add(f"{method} {path} {code}: no schema")
            offenders |= set(_untyped(schema, "response", set()))
    assert offenders - FREE_FORM == set()


def test_every_json_2xx_response_has_an_example() -> None:
    missing = []
    for method, path, op in _operations():
        for code, _ in _json_2xx(op):
            content = op["responses"][code]["content"]["application/json"]
            if "example" not in content and "examples" not in content:
                missing.append(f"{method} {path} {code}")
    assert missing == []


def test_examples_validate_against_their_response_models() -> None:
    checked = 0
    for route in _api_routes():
        if route.response_field is None:
            continue
        for code, response in (route.responses or {}).items():
            example = response.get("content", {}).get("application/json", {}).get("example")
            if example is None or not str(code).startswith("2"):
                continue
            TypeAdapter(route.response_field.field_info.annotation or Any).validate_python(example)
            checked += 1
    assert checked >= 60


@pytest.mark.parametrize("slug", sorted(OBJECTS))
def test_object_examples_validate_as_read_and_create(slug: str) -> None:
    apis = OBJECT_APIS[COLLECTIONS[slug]]
    TypeAdapter(apis.read).validate_python(OBJECTS[slug])
    TypeAdapter(apis.create).validate_python(CREATE_BODIES[slug])


def test_object_reads_document_etag_and_if_match_problems() -> None:
    get = SPEC["paths"]["/api/v1/tenants/{t}/pools/{object_id}"]["get"]
    assert "ETag" in get["responses"]["200"]["headers"]
    patch = SPEC["paths"]["/api/v1/tenants/{t}/pools/{object_id}"]["patch"]
    assert {"412", "428", "422"} <= set(patch["responses"])
    problem = patch["responses"]["412"]["content"]["application/problem+json"]["schema"]
    assert problem["$ref"].endswith("/ProblemOut")


@pytest.mark.parametrize(
    ("slug", "tags"),
    [
        (
            "policy-sets",
            {
                "redirect",
                "allow_list",
                "rewrite",
                "respond",
                "error_pages",
                "rate_limit",
                "access",
                "jwt",
                "client_cert",
                "waf",
                "crowdsec",
                "protection",
                "directory",
                "posture",
            },
        ),
        ("routing-profiles", {"vrrp", "bgp"}),
    ],
)
def test_policy_sets_and_routing_profiles_are_discriminated_unions(
    slug: str, tags: set[str]
) -> None:
    path = SPEC["paths"][f"/api/v1/tenants/{{t}}/{slug}"]
    one = SPEC["paths"][f"/api/v1/tenants/{{t}}/{slug}/{{object_id}}"]
    schemas = [
        path["post"]["requestBody"]["content"]["application/json"]["schema"],
        one["patch"]["requestBody"]["content"]["application/json"]["schema"],
        one["get"]["responses"]["200"]["content"]["application/json"]["schema"],
    ]
    for schema in schemas:
        assert schema["discriminator"]["propertyName"] == "kind"
        assert set(schema["discriminator"]["mapping"]) == tags
        assert len(schema["oneOf"]) == len(tags)


def test_new_field_level_errors_keep_a_plain_path() -> None:
    # the union tag must not leak into errors[].path (see problems.py)
    from switchyard.api.problems import strip_union_tag

    assert strip_union_tag(("body", "redirect", "rules", 0, "redirect", "location")) == (
        "rules.0.redirect.location"
    )
    assert strip_union_tag(("body", "name")) == "name"
    assert strip_union_tag(("query", "limit")) == "limit"


def test_member_status_carries_the_health_check_fields() -> None:
    """The status service reports these; the response model used to forbid them (500 on a real SE)."""
    from switchyard.api.schemas import MemberStatus

    member = MemberStatus.model_validate(
        {
            "backend": "be",
            "server": "s1",
            "status": "UP",
            "check_status": "L7OK",
            "last_change_s": 9,
        }
    )
    assert (member.check_status, member.last_change_s) == ("L7OK", 9)
    assert (
        MemberStatus.model_validate({"backend": "be", "server": "s1", "status": "UP"}).check_status
        is None
    )


def test_a_deploy_request_cannot_carry_a_path_in_a_node_or_pool_name() -> None:
    from pydantic import ValidationError

    from switchyard.api.schemas import DeployIn

    base = {
        "count": 1,
        "nodes": ["pve1"],
        "template_vmid": 9000,
        "mgmt_pool_id": "0192f3a0-0000-7000-8000-000000000001",
    }
    assert DeployIn.model_validate(base).nodes == ["pve1"]
    for nodes in (["../x"], ["pve1/qemu"], ["a b"]):
        with pytest.raises(ValidationError):
            DeployIn.model_validate(base | {"nodes": nodes})
    with pytest.raises(ValidationError):
        DeployIn.model_validate(base | {"pool": "../pool"})
