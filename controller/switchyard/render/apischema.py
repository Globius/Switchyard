# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render an API schema (ADR 0197) as HAProxy request rules: a positive security model.

The rules run in the service's frontend before the WAF. Every value that reaches the
configuration is a number, a member of a fixed set, or a name that the models restrict to
``[A-Za-z0-9._~-]`` and that is escaped here for use inside a regular expression. Regular
expressions are single-quoted, so HAProxy does not expand ``$`` or ``{``.

A path with dot segments, repeated slashes or an encoded dot, slash or backslash is a violation
(``path``): the origin might read it differently from HAProxy. Then:

1. each operation sets ``txn.sy_api`` (its number) when method and path match, and every operation
   sets ``txn.sy_apip`` when only the path matches (to tell 404 from 405);
2. violation rules record the *first* problem in ``txn.sy_apiv``: ``path``, ``method``, ``query``,
   ``param``, ``length``, ``size`` or ``ctype``;
3. the reason is captured for the access log in both modes, and in ``block`` mode mapped to a
   status.

A body schema (ADR 0228) is a second, smaller output: one JSON Schema document per operation that
declares one, written next to the Coraza configuration, and one chained ``SecRule`` per document
that asks Coraza's ``@validateSchema`` operator to check the request body. Both are generated
here from the validated tree; no user text is copied into a file. The documents are named after
the operation's number in the order the HAProxy rules use, so a log line and a file agree.

Example:
    >>> from switchyard.domain.models import ApiOperation, ApiSchema
    >>> schema = ApiSchema(operations=[ApiOperation(method="GET", path="/ping")])
    >>> lines = request_lines(schema)
    >>> any("path -m reg '^/ping$'" in line for line in lines)
    True
"""

import json
import re

from switchyard.domain import models as dm
from switchyard.render import escape as esc

#: Variable that holds the matching operation's number.
OP_VAR = "txn.sy_api"
#: Variable that records that some operation matches the path (any method).
PATH_VAR = "txn.sy_apip"
#: Variable that holds the first violation's reason.
VIOLATION_VAR = "txn.sy_apiv"
#: Where the generated body schemas live on an engine (the agent writes under ``/etc/switchyard``).
BODY_DIR = "/etc/switchyard/coraza/schemas"
#: Directory of the generated body schemas inside an agent bundle.
BODY_BUNDLE_DIR = "coraza/schemas"
#: First Coraza rule id of a generated body rule; the CRS uses ids from 900000 up.
BODY_RULE_ID_BASE = 100_000

_STATUS = {
    "path": 404,
    "method": 405,
    "query": 400,
    "param": 400,
    "length": 411,
    "size": 413,
    "ctype": 415,
}
_CUSTOM = {
    411: "length required",
    413: "request body too large",
    415: "unsupported media type",
}
_UUID = "[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"


def _literal(text: str) -> str:
    """Escape a validated name for use in a regular expression.

    Example:
        >>> _literal("v1.2-a_b~c")
        'v1[.]2-a_b~c'
    """
    if not re.fullmatch(r"[A-Za-z0-9._~-]+", text):
        raise esc.RenderError(f"unsafe name {text!r}")
    return text.replace(".", "[.]")


def value_regex(param: dm.ApiParam, *, in_path: bool) -> str:
    """The regular expression a parameter value must match (anchors are the caller's).

    Example:
        >>> value_regex(dm.ApiParam(name="id", kind="integer"), in_path=True)
        '-?[0-9]{1,18}'
        >>> value_regex(dm.ApiParam(name="s", kind="enum", values=("a", "b.c")), in_path=False)
        '(a|b[.]c)'
    """
    if param.kind == "integer":
        return "-?[0-9]{1,18}"
    if param.kind == "number":
        return "-?[0-9]{1,18}([.][0-9]{1,18})?"
    if param.kind == "boolean":
        return "(true|false)"
    if param.kind == "uuid":
        return _UUID
    if param.kind == "enum":
        return "(" + "|".join(_literal(v) for v in param.values) + ")"
    limit = int(param.max_length)
    return f"[^/]{{1,{limit}}}" if in_path else f"[^&]{{0,{limit}}}"


def path_regex(schema: dm.ApiSchema, op: dm.ApiOperation) -> str:
    """The anchored regular expression the path of an operation must match.

    Example:
        >>> op = dm.ApiOperation(method="GET", path="/pets/{id}",
        ...     path_params=[dm.ApiParam(name="id", kind="integer")])
        >>> path_regex(dm.ApiSchema(base_path="/v1", operations=[op]), op)
        '^/v1/pets/(-?[0-9]{1,18})$'
    """
    typed = {p.name: p for p in op.path_params}
    out = ""
    for part in (schema.base_path + op.path).split("/")[1:]:
        match = re.fullmatch(r"\{([A-Za-z0-9_]+)\}", part)
        if match:
            param = typed.get(match.group(1)) or dm.ApiParam(name=match.group(1))
            out += f"/({value_regex(param, in_path=True)})"
        else:
            out += "/" + _literal(part)
    return f"^{out}$"


def query_regex(op: dm.ApiOperation) -> str:
    """A regular expression for the whole query string: every pair is a listed parameter.

    Because the expression covers every ``&``-separated pair, a repeated parameter cannot carry
    a value that the first occurrence hides.

    Example:
        >>> op = dm.ApiOperation(method="GET", path="/pets", query=[
        ...     dm.ApiParam(name="limit", kind="integer"), dm.ApiParam(name="q")])
        >>> query_regex(op)
        '^(limit=-?[0-9]{1,18}|q(=[^&]{0,256})?)(&(limit=-?[0-9]{1,18}|q(=[^&]{0,256})?))*$'
    """
    pairs = []
    for param in op.query:
        name = _literal(param.name)
        value = value_regex(param, in_path=False)
        pairs.append(f"{name}(={value})?" if param.kind == "string" else f"{name}={value}")
    alternation = "|".join(pairs)
    return f"^({alternation})(&({alternation}))*$"


def _specificity(op: dm.ApiOperation) -> int:
    return sum(1 for part in op.path.split("/")[1:] if not part.startswith("{"))


def _q(pattern: str) -> str:
    if "'" in pattern or "\n" in pattern:
        raise esc.RenderError("unsafe pattern")
    return f"'{pattern}'"


def ordered_operations(schema: dm.ApiSchema) -> list[tuple[int, dm.ApiOperation]]:
    """The operations numbered from 1, most specific path first, ties in declared order.

    The number is what ``txn.sy_api`` carries and what names a generated body schema, so the two
    outputs of this module agree.

    Example:
        >>> schema = dm.ApiSchema(operations=[
        ...     dm.ApiOperation(method="GET", path="/orders/{ref}"),
        ...     dm.ApiOperation(method="GET", path="/orders/mine")])
        >>> [(number, op.path) for number, op in ordered_operations(schema)]
        [(1, '/orders/mine'), (2, '/orders/{ref}')]
    """
    ordered = sorted(
        enumerate(schema.operations), key=lambda item: (-_specificity(item[1]), item[0])
    )
    return [(number, op) for number, (_, op) in enumerate(ordered, start=1)]


def request_lines(schema: dm.ApiSchema) -> list[str]:
    """The ``http-request`` rules that enforce ``schema`` in a service's frontend.

    Example:
        >>> from switchyard.domain.models import ApiOperation, ApiSchema
        >>> lines = request_lines(ApiSchema(operations=[ApiOperation(method="GET", path="/ping")],
        ...     mode="block"))
        >>> lines[-1].startswith("http-request return status 415 content-type text/plain")
        True
    """
    ordered = ordered_operations(schema)
    lines: list[str] = []
    for number, op in ordered:
        regex = _q(path_regex(schema, op))
        lines.append(f"http-request set-var({PATH_VAR}) int(1) if {{ path -m reg {regex} }}")
        lines.append(
            f"http-request set-var({OP_VAR}) int({number}) if {{ method {op.method} }} "
            f"{{ path -m reg {regex} }} !{{ var({OP_VAR}) -m found }}"
        )
    first = f"!{{ var({VIOLATION_VAR}) -m found }}"

    def violation(reason: str, condition: str) -> None:
        lines.append(f"http-request set-var({VIOLATION_VAR}) str({reason}) if {first} {condition}")

    # What is checked must be what the origin receives: refuse dot segments, repeated slashes and
    # encoded dots, slashes and backslashes, which an origin may decode differently.
    violation("path", "{ path -m reg '(^|/)[.]{1,2}(/|$)' }")
    violation("path", "{ path -m reg '//|%2[eEfF]|%5[cC]' }")
    violation("path", f"!{{ var({PATH_VAR}) -m found }}")
    violation("method", f"{{ var({PATH_VAR}) -m found }} !{{ var({OP_VAR}) -m found }}")
    for number, op in ordered:
        here = f"{{ var({OP_VAR}) -m int {number} }}"
        if not op.query and not schema.allow_unknown_query:
            violation("query", f"{here} {{ query -m reg '.' }}")
        elif op.query and not schema.allow_unknown_query:
            violation(
                "query", f"{here} {{ query -m reg '.' }} !{{ query -m reg {_q(query_regex(op))} }}"
            )
        for param in op.query:
            fetch = f"url_param({esc.arg(param.name)})"
            if param.required:
                violation("param", f"{here} !{{ {fetch} -m found }}")
            if schema.allow_unknown_query:
                regex = _q(f"^{value_regex(param, in_path=False)}$")
                violation("param", f"{here} {{ {fetch} -m found }} !{{ {fetch} -m reg {regex} }}")
        if op.content_types:
            types = " ".join(esc.quoted(c) for c in sorted(op.content_types))
            wrong = f"!{{ req.hdr(content-type),field(1,;),lower -m str {types} }}"
            violation("ctype", f"{here} {{ req.hdr_val(content-length) gt 0 }} {wrong}")
            violation("ctype", f"{here} {{ req.hdr(transfer-encoding) -m found }} {wrong}")
        limit = op.max_body_bytes if op.max_body_bytes is not None else schema.max_body_bytes
        if limit is not None:
            violation("length", f"{here} {{ req.hdr(transfer-encoding) -m found }}")
            violation("size", f"{here} {{ req.hdr_val(content-length) gt {int(limit)} }}")
    lines.append(f"http-request capture var({VIOLATION_VAR}) len 8")
    if schema.mode == "block":
        by_status: dict[int, list[str]] = {}
        for reason, status in _STATUS.items():
            by_status.setdefault(status, []).append(reason)
        for status, reasons in sorted(by_status.items()):
            names = " ".join(reasons)
            test = f"{{ var({VIOLATION_VAR}) -m str {names} }}"
            if status in _CUSTOM:
                # HAProxy has no built-in error page for these statuses
                lines.append(
                    f"http-request return status {status} content-type text/plain "
                    f'string "{_CUSTOM[status]}" if {test}'
                )
            else:
                lines.append(f"http-request deny deny_status {status} if {test}")
    return lines


_SCHEMA_TYPE = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
    "uuid": "string",
    "enum": "string",
}

#: A rendered path may only carry the characters a validated template and base path use.
_PATH_TEXT = re.compile(r"[A-Za-z0-9._~/{}-]+")


def _path_text(value: str) -> str:
    """Return ``value`` if it is safe inside a comment line, else raise.

    Example:
        >>> _path_text("/v1/pets/{id}")
        '/v1/pets/{id}'
    """
    if not _PATH_TEXT.fullmatch(value):
        raise esc.RenderError(f"unsafe path {value!r}")
    return value


def _property_schema(prop: dm.ApiBodyProperty) -> dict[str, object]:
    """The JSON Schema of one body property (no ``$ref``, no remote document).

    Example:
        >>> _property_schema(dm.ApiBodyProperty(name="n", kind="enum", values=("a", "b")))
        {'type': 'string', 'enum': ['a', 'b']}
    """
    if prop.kind == "object":
        return _object_schema(prop.properties, additional=prop.additional_properties)
    if prop.kind == "array":
        if prop.items is None:  # the model requires items with kind array
            raise esc.RenderError(f"array property {prop.name!r} has no item type")
        item: dict[str, object] = {"type": _SCHEMA_TYPE[prop.items]}
        if prop.items == "string":
            item["maxLength"] = int(prop.max_length)
        if prop.items == "uuid":
            item["pattern"] = _UUID
        out: dict[str, object] = {"type": "array", "items": item}
        if prop.max_items is not None:
            out["maxItems"] = int(prop.max_items)
        return out
    if prop.kind == "enum":
        return {"type": "string", "enum": list(prop.values)}
    if prop.kind == "uuid":
        return {"type": "string", "pattern": _UUID}
    if prop.kind == "string":
        return {"type": "string", "maxLength": int(prop.max_length)}
    return {"type": _SCHEMA_TYPE[prop.kind]}


def _object_schema(
    properties: tuple[dm.ApiBodyProperty, ...], *, additional: bool
) -> dict[str, object]:
    """The JSON Schema of an object with ``properties``; declared order is kept."""
    return {
        "type": "object",
        "additionalProperties": additional,
        "required": [p.name for p in properties if p.required],
        "properties": {p.name: _property_schema(p) for p in properties},
    }


def body_schema(op: dm.ApiOperation) -> dict[str, object]:
    """The JSON Schema document Coraza validates the body of ``op`` against.

    Raises:
        switchyard.render.escape.RenderError: If the operation has no ``body``.

    Example:
        >>> op = dm.ApiOperation(method="POST", path="/pets",
        ...     content_types=("application/json",),
        ...     body=dm.ApiBody(properties=(dm.ApiBodyProperty(name="sku", required=True),)))
        >>> body_schema(op)["required"]
        ['sku']
    """
    if op.body is None:
        raise esc.RenderError(f"{op.method} {op.path} has no body schema")
    return _object_schema(op.body.properties, additional=op.body.additional_properties)


def body_file_name(service: str, number: int) -> str:
    """Bundle-relative path of the body schema of operation ``number`` of a service.

    Example:
        >>> body_file_name("pets", 3)
        'coraza/schemas/pets-3.json'
    """
    return f"{BODY_BUNDLE_DIR}/{esc.ident(service)}-{int(number)}.json"


def body_files(service: str, schema: dm.ApiSchema) -> dict[str, str]:
    """The JSON Schema files a service's API schema needs, by bundle-relative path.

    One file per operation that declares a ``body``; the name carries the operation's number.

    Example:
        >>> schema = dm.ApiSchema(operations=[dm.ApiOperation(method="POST", path="/pets",
        ...     content_types=("application/json",),
        ...     body=dm.ApiBody(properties=(dm.ApiBodyProperty(name="sku"),)))])
        >>> list(body_files("pets", schema))
        ['coraza/schemas/pets-1.json']
    """
    files: dict[str, str] = {}
    for number, op in ordered_operations(schema):
        if op.body is None:
            continue
        files[body_file_name(service, number)] = (
            json.dumps(body_schema(op), indent=2, ensure_ascii=True) + "\n"
        )
    return files


def body_directives(service: str, schema: dm.ApiSchema) -> list[str]:
    """The ``SecRule`` chain per operation with a body, for the service's Coraza directives.

    Coraza's JSON body processor only runs for ``application/json``, so an operation that reaches
    here declares that media type. ``@validateSchema`` answers true when the body does not match,
    so the rule fires on a violation: ``deny`` with 400 in ``block`` mode, ``pass`` (log only) in
    ``detect`` mode. Coraza takes the disruptive action from the *first* rule of a chain, so it
    sits there; the checks follow as chained rules. The rule id is ``BODY_RULE_ID_BASE`` plus the
    operation number.

    Example:
        >>> schema = dm.ApiSchema(mode="block", operations=[dm.ApiOperation(method="POST",
        ...     path="/pets", content_types=("application/json",),
        ...     body=dm.ApiBody(properties=(dm.ApiBodyProperty(name="sku"),)))])
        >>> body_directives("pets", schema)[-1]
        'SecRule REQUEST_BODY "@validateSchema /etc/switchyard/coraza/schemas/pets-1.json" "t:none"'
    """
    ident = esc.ident(service)
    action = "deny,status:400" if schema.mode == "block" else "pass"
    lines: list[str] = []
    for number, op in ordered_operations(schema):
        if op.body is None:
            continue
        path = f"{BODY_DIR}/{ident}-{number}.json"
        lines.append(
            f"# API body schema: {op.method} {_path_text(schema.base_path + op.path)} "
            f"(operation {number})"
        )
        lines.append(
            f'SecRule REQUEST_METHOD "@streq {op.method}" '
            f'"id:{BODY_RULE_ID_BASE + number},phase:2,rev:1,log,auditlog,chain,'
            f"msg:'api body schema violation',tag:'switchyard/api-body',"
            f"logdata:'%{{REQUEST_METHOD}} %{{REQUEST_FILENAME}}',{action}\""
        )
        lines.append(f'SecRule REQUEST_FILENAME "@rx {path_regex(schema, op)}" "chain,t:none"')
        lines.append(f'SecRule REQUEST_BODY "@validateSchema {path}" "t:none"')
    return lines
