# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Turn an OpenAPI document into a WAF API schema (ADR 0197).

Only what the schema can enforce is read: the paths and methods, the types of path and query
parameters, whether query parameters are required, and the media types of request bodies.
Everything else (response schemas, headers, JSON body schemas, security) is ignored. What cannot be
represented is reported in ``warnings`` and left out, never guessed: an operation that is not in the
result is refused by the schema once it is switched to ``block``.

Example:
    >>> doc = '''
    ... openapi: 3.0.3
    ... servers: [{url: "https://api.example.test/v1"}]
    ... paths:
    ...   /pets/{id}:
    ...     get:
    ...       parameters:
    ...         - {name: id, in: path, required: true, schema: {type: integer}}
    ... '''
    >>> schema, warnings = api_schema_from_openapi(doc)
    >>> schema.base_path, schema.operations[0].path, schema.operations[0].path_params[0].kind
    ('/v1', '/pets/{id}', 'integer')
"""

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError

from switchyard.domain import models as dm

_METHODS = {m.lower(): m for m in dm.API_METHODS}
_TOKEN = re.compile(r"[A-Za-z0-9._~-]{1,64}")
_MEDIA = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,62}/[a-z0-9][a-z0-9!#$&^_.+-]{0,62}")
_PLACEHOLDER = re.compile(r"\{([^{}/]+)\}")
MAX_DOCUMENT_BYTES = 4 * 1024 * 1024


class OpenApiError(ValueError):
    """The document cannot be read as an OpenAPI description."""


def _safe_name(name: str) -> str:
    """A placeholder name the models accept (letters, digits, ``_``; no leading digit)."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)[:32]
    return cleaned if re.match(r"[A-Za-z_]", cleaned) else f"p_{cleaned}"[:32]


def _resolve(document: Mapping[str, Any], item: Any) -> Mapping[str, Any]:
    """Follow a local ``$ref`` (``#/a/b``); remote references are an error."""
    seen = 0
    while isinstance(item, Mapping) and "$ref" in item:
        ref = item["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or seen > 8:
            raise OpenApiError(f"only local references are supported, not {ref!r}")
        node: Any = document
        for part in ref[2:].split("/"):
            node = (
                node.get(part.replace("~1", "/").replace("~0", "~"))
                if isinstance(node, Mapping)
                else None
            )
        item, seen = node, seen + 1
    if not isinstance(item, Mapping):
        raise OpenApiError("a reference does not lead to an object")
    return item


def _kind(schema: Mapping[str, Any]) -> tuple[str, tuple[str, ...], int]:
    """Map a parameter schema to ``(kind, enum values, max length)``."""
    if schema.get("type") == "array" and isinstance(schema.get("items"), Mapping):
        schema = schema["items"]
    enum = schema.get("enum")
    if (
        isinstance(enum, list)
        and enum
        and all(isinstance(v, str) and _TOKEN.fullmatch(v) for v in enum)
    ):
        return "enum", tuple(dict.fromkeys(enum))[:32], 256
    kind = schema.get("type")
    if kind in ("integer", "number", "boolean"):
        return str(kind), (), 256
    if kind == "string" and schema.get("format") == "uuid":
        return "uuid", (), 256
    limit = schema.get("maxLength")
    return "string", (), min(int(limit), 2048) if isinstance(limit, int) and limit > 0 else 256


def _param(document: Mapping[str, Any], raw: Any) -> tuple[str, str, dm.ApiParam] | None:
    item = _resolve(document, raw)
    location, name = item.get("in"), item.get("name")
    if location not in ("path", "query") or not isinstance(name, str):
        return None
    declared = item.get("schema")
    kind, values, limit = _kind(declared if isinstance(declared, Mapping) else item)
    try:
        param = dm.ApiParam(
            name=_safe_name(name) if location == "path" else name,
            kind=kind,
            required=bool(item.get("required")) and location == "query",
            values=values,
            max_length=limit,
        )
    except ValidationError as exc:
        raise OpenApiError(f"parameter {name!r}: {exc.errors()[0]['msg']}") from exc
    return str(location), name, param


def _base_path(document: Mapping[str, Any]) -> str:
    if "basePath" in document:  # Swagger 2.0
        raw = str(document["basePath"])
    else:
        servers = document.get("servers")
        raw = ""
        if isinstance(servers, list) and servers and isinstance(servers[0], Mapping):
            raw = urlsplit(str(servers[0].get("url", ""))).path
    raw = raw.rstrip("/")
    return raw if re.fullmatch(r"(/[A-Za-z0-9._~-]{1,64}){0,4}", raw) else ""


def _content_types(document: Mapping[str, Any], operation: Mapping[str, Any]) -> tuple[str, ...]:
    found: list[str] = []
    body = operation.get("requestBody")
    if body is not None:
        content = _resolve(document, body).get("content")
        found = list(content) if isinstance(content, Mapping) else []
    elif isinstance(operation.get("consumes"), list):
        found = list(operation["consumes"])
    elif isinstance(document.get("consumes"), list) and operation.get("parameters"):
        found = []  # a document-wide default is not tied to this operation having a body
    types = [str(t).split(";")[0].strip().lower() for t in found]
    return tuple(dict.fromkeys(t for t in types if _MEDIA.fullmatch(t)))[:8]


def api_schema_from_openapi(
    source: str | Mapping[str, Any],
    *,
    mode: str = "detect",
    base_path: str | None = None,
    allow_unknown_query: bool = False,
    max_body_bytes: int | None = None,
) -> tuple[dm.ApiSchema, list[str]]:
    """Build an :class:`~switchyard.domain.models.ApiSchema` from OpenAPI 3.x or Swagger 2.0.

    Args:
        source: The document (YAML or JSON text) or the parsed mapping.
        mode: ``detect`` (default) or ``block`` for the result.
        base_path: Overrides the prefix taken from ``servers`` / ``basePath``.
        allow_unknown_query: Passed to the schema.
        max_body_bytes: Passed to the schema.

    Returns:
        The schema and a list of warnings for what was left out.

    Raises:
        OpenApiError: If the document is not readable, has no usable operation, or has more than
            500 operations.
    """
    if isinstance(source, str):
        if len(source.encode()) > MAX_DOCUMENT_BYTES:
            raise OpenApiError("the document is larger than 4 MiB")
        try:
            document = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            raise OpenApiError(f"cannot parse the document: {exc}") from exc
    else:
        document = source
    if not isinstance(document, Mapping) or not (
        str(document.get("openapi", "")).startswith("3.") or document.get("swagger") == "2.0"
    ):
        raise OpenApiError("not an OpenAPI 3.x or Swagger 2.0 document")
    paths = document.get("paths")
    if not isinstance(paths, Mapping) or not paths:
        raise OpenApiError("the document has no paths")
    warnings: list[str] = []
    operations: list[dm.ApiOperation] = []
    for template, item in paths.items():
        if not isinstance(item, Mapping) or not isinstance(template, str):
            continue
        parts = template.split("/")[1:] if template.startswith("/") else []
        if not parts or not all(_TOKEN.fullmatch(p) or _PLACEHOLDER.fullmatch(p) for p in parts):
            warnings.append(f"{template}: the path has characters a schema cannot hold; skipped")
            continue
        safe_path = "/" + "/".join(
            "{" + _safe_name(m.group(1)) + "}" if (m := _PLACEHOLDER.fullmatch(p)) else p
            for p in parts
        )
        shared = item.get("parameters", [])
        for key, method in _METHODS.items():
            operation = item.get(key)
            if not isinstance(operation, Mapping):
                continue
            path_params: dict[str, dm.ApiParam] = {}
            query: dict[str, dm.ApiParam] = {}
            declared = [
                *(shared if isinstance(shared, list) else []),
                *operation.get("parameters", []),
            ]
            for raw in declared:
                try:
                    read = _param(document, raw)
                except OpenApiError as exc:
                    warnings.append(f"{method} {template}: {exc}; parameter left out")
                    continue
                if read is not None:
                    (path_params if read[0] == "path" else query)[read[2].name] = read[2]
            try:
                operations.append(
                    dm.ApiOperation(
                        method=method,
                        path=safe_path,
                        path_params=tuple(
                            p
                            for p in path_params.values()
                            if p.name in _PLACEHOLDER.findall(safe_path)
                        ),
                        query=tuple(list(query.values())[:32]),
                        content_types=_content_types(document, operation),
                    )
                )
            except ValidationError as exc:
                warnings.append(f"{method} {template}: {exc.errors()[0]['msg']}; skipped")
            if len(query) > 32:
                warnings.append(
                    f"{method} {template}: more than 32 query parameters; extra ones left out"
                )
    if not operations:
        raise OpenApiError("no operation could be turned into a schema rule")
    if len(operations) > 500:
        raise OpenApiError(f"{len(operations)} operations; a schema holds at most 500")
    base = base_path if base_path is not None else _base_path(document)
    try:
        schema = dm.ApiSchema(
            mode=mode,
            operations=tuple(operations),
            base_path=base,
            allow_unknown_query=allow_unknown_query,
            max_body_bytes=max_body_bytes,
        )
    except ValidationError as exc:
        raise OpenApiError(str(exc.errors()[0]["msg"])) from exc
    return schema, warnings
