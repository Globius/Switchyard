# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Turning an OpenAPI document into a WAF API schema (ADR 0197)."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from switchyard.cli import app
from switchyard.importers.openapi import OpenApiError, api_schema_from_openapi

DOC = """
openapi: 3.0.3
info: {title: pets, version: "1"}
servers: [{url: "https://api.example.test/v1/"}]
components:
  parameters:
    Limit: {name: limit, in: query, schema: {type: integer, maximum: 100}}
paths:
  /pets:
    get:
      parameters:
        - $ref: '#/components/parameters/Limit'
        - {name: status, in: query, required: true, schema: {type: string, enum: [available, sold]}}
        - {name: tags, in: query, schema: {type: array, items: {type: integer}}}
        - {name: X-Trace, in: header, schema: {type: string}}
        - {name: sid, in: cookie, schema: {type: string}}
    post:
      requestBody:
        content:
          application/json: {}
          Application/XML; charset=utf-8: {}
          not a type: {}
  /pets/{pet-id}:
    parameters:
      - {name: pet-id, in: path, required: true, schema: {type: string, format: uuid}}
    get: {}
    delete: {}
    trace: {}
  /pets/{pet-id}/photos.json:
    get:
      parameters:
        - {name: size, in: query, schema: {type: string, maxLength: 99999}}
  /a b:
    get: {}
  /files/{p}:
    get:
      parameters:
        - {name: "bad name", in: query, schema: {type: string}}
"""


def test_operations_parameters_and_body_types() -> None:
    schema, warnings = api_schema_from_openapi(DOC, mode="block")
    assert schema.mode == "block" and schema.base_path == "/v1"
    by = {(o.method, o.path): o for o in schema.operations}
    get = by["GET", "/pets"]
    assert [(q.name, q.kind, q.required) for q in get.query] == [
        ("limit", "integer", False),
        ("status", "enum", True),
        ("tags", "integer", False),  # an array is checked by its item type
    ]
    assert get.query[1].values == ("available", "sold")
    assert by["POST", "/pets"].content_types == ("application/json", "application/xml")
    # a name with a dash becomes a legal placeholder, in the path and in its type
    assert ("GET", "/pets/{pet_id}") in by and by["DELETE", "/pets/{pet_id}"].path_params[
        0
    ].kind == "uuid"
    assert ("TRACE", "/pets/{pet_id}") not in by
    assert by["GET", "/pets/{pet_id}/photos.json"].query[0].max_length == 2048
    assert any("/a b" in w for w in warnings)
    assert any("bad name" in w for w in warnings)  # the parameter is left out, the operation kept
    assert by["GET", "/files/{p}"].query == ()


def test_swagger_2_and_overrides() -> None:
    doc = {
        "swagger": "2.0",
        "basePath": "/api",
        "paths": {"/ping": {"get": {}}, "/up": {"post": {"consumes": ["application/json"]}}},
    }
    schema, _ = api_schema_from_openapi(
        doc, base_path="", allow_unknown_query=True, max_body_bytes=5
    )
    assert schema.base_path == "" and schema.allow_unknown_query and schema.max_body_bytes == 5
    assert {o.path for o in schema.operations} == {"/ping", "/up"}
    assert api_schema_from_openapi(doc)[0].base_path == "/api"


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("{not yaml", "cannot parse"),
        ("[1, 2]", "not an OpenAPI"),
        ({"openapi": "3.0.0"}, "no paths"),
        ({"openapi": "3.0.0", "paths": {"/a b": {"get": {}}}}, "no operation"),
        pytest.param("x" * (4 * 1024 * 1024 + 1), "larger than", id="too-large"),
    ],
)
def test_unusable_documents(source: object, message: str) -> None:
    with pytest.raises(OpenApiError, match=message):
        api_schema_from_openapi(source)  # type: ignore[arg-type]


def test_a_remote_reference_is_left_out_with_a_warning() -> None:
    doc = {
        "openapi": "3.0.0",
        "paths": {"/a": {"get": {"parameters": [{"$ref": "https://evil.example.test/x"}]}}},
    }
    schema, warnings = api_schema_from_openapi(doc)
    assert schema.operations[0].query == () and any("local references" in w for w in warnings)


def test_too_many_operations() -> None:
    doc = {"openapi": "3.0.0", "paths": {f"/p{i}": {"get": {}} for i in range(501)}}
    with pytest.raises(OpenApiError, match="at most 500"):
        api_schema_from_openapi(doc)


def test_cli_prints_the_api_part(tmp_path: Path) -> None:
    file = tmp_path / "pets.yaml"
    file.write_text(DOC)
    result = CliRunner().invoke(app, ["waf", "api-schema", str(file), "--mode", "block"])
    assert result.exit_code == 0, result.output
    start = result.stdout.index("{")
    out = json.loads(result.stdout[start:])
    assert out["api"]["mode"] == "block" and out["api"]["base_path"] == "/v1"


def test_cli_errors(tmp_path: Path) -> None:
    file = tmp_path / "x.yaml"
    file.write_text("hello: world")
    runner = CliRunner()
    assert runner.invoke(app, ["waf", "api-schema", str(file)]).exit_code == 2
    assert runner.invoke(app, ["waf", "api-schema", str(file), "--mode", "off"]).exit_code == 2
    assert runner.invoke(app, ["waf", "api-schema", str(tmp_path / "missing.yaml")]).exit_code == 2
