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

Example:
    >>> from switchyard.domain.models import ApiOperation, ApiSchema
    >>> schema = ApiSchema(operations=[ApiOperation(method="GET", path="/ping")])
    >>> lines = request_lines(schema)
    >>> any("path -m reg '^/ping$'" in line for line in lines)
    True
"""

import re

from switchyard.domain import models as dm
from switchyard.render import escape as esc

#: Variable that holds the matching operation's number.
OP_VAR = "txn.sy_api"
#: Variable that records that some operation matches the path (any method).
PATH_VAR = "txn.sy_apip"
#: Variable that holds the first violation's reason.
VIOLATION_VAR = "txn.sy_apiv"

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


def request_lines(schema: dm.ApiSchema) -> list[str]:
    """The ``http-request`` rules that enforce ``schema`` in a service's frontend.

    Example:
        >>> from switchyard.domain.models import ApiOperation, ApiSchema
        >>> lines = request_lines(ApiSchema(operations=[ApiOperation(method="GET", path="/ping")],
        ...     mode="block"))
        >>> lines[-1].startswith("http-request return status 415 content-type text/plain")
        True
    """
    ordered = sorted(
        enumerate(schema.operations), key=lambda item: (-_specificity(item[1]), item[0])
    )
    lines: list[str] = []
    for number, (_, op) in enumerate(ordered, start=1):
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
    for number, (_, op) in enumerate(ordered, start=1):
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
