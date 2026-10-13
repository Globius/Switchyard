# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Every operation is documented well enough to design and generate a client against."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from switchyard.api import docs_meta
from switchyard.api.app import COLLECTIONS, create_app
from switchyard.api.schemas import OBJECT_APIS

SPEC: dict[str, Any] = create_app(None).openapi()  # type: ignore[arg-type]
METHODS = ("get", "post", "put", "patch", "delete")
OPS = [
    (m.upper(), p.removeprefix("/api/v1"), op)
    for p, item in SPEC["paths"].items()
    for m, op in item.items()
    if m in METHODS
]
IDS = [f"{m} {p}" for m, p, _ in OPS]
#: Responses that are not JSON bodies: a stream, a redirect, an XML document.
NO_JSON_EXAMPLE = {
    "GET /tenants/{t}/status/stream",
    "GET /auth/saml/{provider}/metadata",
    "GET /tenants/{t}/captures/{capture}/pcap",
    "GET /system/bundles/{bundle}/file",
}
#: Form posts and operations without a request body example for a documented reason.
NO_BODY_EXAMPLE = {"POST /auth/callback/{provider}"}


@pytest.mark.parametrize(("method", "path", "op"), OPS, ids=IDS)
def test_operation_is_documented(method: str, path: str, op: dict[str, Any]) -> None:
    where = f"{method} {path}"
    assert op.get("summary"), where
    assert len(op["description"].split("**Access:**")[0].strip()) >= 25, f"{where}: describe it"
    assert op["x-required-role"] in {
        "public",
        "authenticated",
        "viewer",
        "operator",
        "tenant-admin",
        "system-admin",
    }, where
    assert op["tags"], where
    for param in op.get("parameters", []):
        assert param.get("description"), f"{where}: parameter {param['name']} has no description"
    codes = set(op["responses"])
    if op["x-required-role"] != "public":
        assert {"401", "403"} <= codes, f"{where}: document 401 and 403"
    if "{" in path.replace("{t}", ""):
        assert "404" in codes, f"{where}: document 404"
    for code, response in op["responses"].items():
        if code.startswith(("4", "5")):
            assert "application/problem+json" in response.get("content", {}), f"{where} {code}"
    successes = [c for c in codes if c.startswith(("2", "3"))]
    assert successes, where
    if where not in NO_JSON_EXAMPLE:
        for code in successes:
            if code == "204" or code.startswith("3"):
                continue
            assert "example" in json.dumps(op["responses"][code]), f"{where}: example for {code}"
    body = op.get("requestBody")
    if body and where not in NO_BODY_EXAMPLE:
        assert "example" in json.dumps(body), f"{where}: request example"
    if method in {"POST", "PUT", "PATCH", "DELETE"} and path.startswith("/tenants/{t}"):
        assert "409" in codes or "422" in codes, f"{where}: document conflicts"


def test_security_is_declared() -> None:
    assert set(SPEC["components"]["securitySchemes"]) == {
        "bearerAuth",
        "sessionCookie",
        "csrfToken",
    }
    assert SPEC["security"]
    assert SPEC["paths"]["/api/v1/healthz"]["get"]["security"] == []
    assert {t["name"] for t in SPEC["tags"]} >= {t for _, _, op in OPS for t in op["tags"]}, (
        "every tag needs a description in docs_meta.TAGS"
    )


def test_every_route_has_an_access_rule() -> None:
    for method, path, _ in OPS:
        docs_meta.access_for(method, path)


@pytest.mark.parametrize("slug", sorted(COLLECTIONS))
def test_patch_examples_are_valid(slug: str) -> None:
    example = docs_meta.request_example("PATCH", f"/tenants/{{t}}/{slug}/{{object_id}}")
    assert example is not None
    TypeAdapter(OBJECT_APIS[COLLECTIONS[slug]].patch).validate_python(example)


def test_docs_are_served() -> None:
    from fastapi.testclient import TestClient

    client = TestClient(create_app(None))  # type: ignore[arg-type]
    assert client.get("/api/v1/docs").status_code == 200
    assert client.get("/api/v1/redoc").status_code == 200
    assert client.get("/api/v1/openapi.json").json()["info"]["title"] == "Switchyard API"
    committed = Path(__file__).parents[3] / "docs" / "api" / "openapi.json"
    assert committed.exists()
