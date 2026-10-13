# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tenant scoping of Prometheus and Loki requests (ADR 0085).

Grafana runs queries as itself, so the tenant boundary cannot live in Grafana. Every request a
tenant's Grafana organization sends to a data source goes through the controller, which rewrites
it here: each vector selector (PromQL) and each stream selector (LogQL) gets ``tenant="<id>"`` as
its *first* matcher, bare metric names get a selector, and every endpoint that is not on an allow
list is refused. Matchers are ANDed, so a caller's own ``tenant`` matcher can only narrow results.

The scanner is deliberately small and fails closed: anything it does not understand (an unterminated
string, a character outside ASCII, a quote character LogQL does not have) raises
:class:`ScopeError` and the request is not forwarded. After rewriting, the result is scanned again
and every selector must start with the tenant matcher, so a bug in the first pass cannot silently
pass a selector through.

Example:
    >>> from uuid import UUID
    >>> t = UUID("11111111-1111-1111-1111-111111111111")
    >>> scope_promql('rate(requests_total[1m])', t)
    'rate(requests_total{tenant="11111111-1111-1111-1111-111111111111"}[1m])'
    >>> scope_logql('{frontend="vs_shop"} |= "x"', t)
    '{tenant="11111111-1111-1111-1111-111111111111",frontend="vs_shop"} |= "x"'
"""

import re
from uuid import UUID


class ScopeError(ValueError):
    """The query cannot be scoped safely, or the endpoint is not allowed. Never forward it."""


#: These keywords are followed by a parenthesised list of label names.
_LABEL_KEYWORDS = frozenset({"by", "without", "on", "ignoring", "group_left", "group_right"})
#: Words that are neither functions nor metric names.
_OTHER_KEYWORDS = frozenset({"bool", "offset", "and", "or", "unless", "atan2", "inf", "nan"})
#: Aggregation operators; ``sum by (x) (...)`` has a keyword, not ``(``, after the operator.
_AGGREGATIONS = frozenset(
    {
        "sum",
        "min",
        "max",
        "avg",
        "group",
        "stddev",
        "stdvar",
        "count",
        "count_values",
        "bottomk",
        "topk",
        "quantile",
        "limitk",
        "limit_ratio",
    }
)

_IDENT = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*")
_NUMBER = re.compile(r"[0-9.][0-9A-Za-z_.]*")


def tenant_matcher(tenant_id: UUID) -> str:
    """The matcher that is added to every selector (the tenant id is a UUID, nothing to escape)."""
    return f'tenant="{tenant_id}"'


def _string_end(expr: str, start: int) -> int:
    quote = expr[start]
    i = start + 1
    n = len(expr)
    while i < n:
        c = expr[i]
        if quote != "`" and c == "\\":
            i += 2
            continue
        if c == quote:
            return i + 1
        i += 1
    raise ScopeError("unterminated string in query")


def _next_significant(expr: str, i: int) -> str:
    """The character of the next token after position ``i`` (skipping blanks and comments)."""
    n = len(expr)
    while i < n:
        c = expr[i]
        if c in " \t\r\n":
            i += 1
        elif c == "#":
            j = expr.find("\n", i)
            i = n if j < 0 else j
        else:
            return c
    return ""


def _next_word(expr: str, i: int) -> str:
    while i < len(expr) and expr[i] in " \t\r\n":
        i += 1
    m = _IDENT.match(expr, i)
    return m.group().lower() if m else ""


def _scan(expr: str, matcher: str, *, promql: bool, check: bool) -> str:
    """Rewrite ``expr``; with ``check``, verify it is already scoped and return it unchanged."""
    out: list[str] = []
    n = len(expr)
    i = 0
    pending_labels = False
    label_depth = 0
    in_block = False
    bracket = 0
    while i < n:
        c = expr[i]
        if ord(c) > 126 or (ord(c) < 32 and c not in "\t\r\n"):
            raise ScopeError("query contains a character outside printable ASCII")
        if c in "\"'`":
            if c == "'" and not promql:
                raise ScopeError("LogQL has no single-quoted strings")
            j = _string_end(expr, i)
            out.append(expr[i:j])
            i = j
            continue
        if c == "#":
            j = expr.find("\n", i)
            j = n if j < 0 else j
            out.append(expr[i:j])
            i = j
            continue
        if c in " \t\r\n":
            out.append(c)
            i += 1
            continue
        if c == "{":
            if check:
                end = i + 1 + len(matcher)
                if not expr.startswith("{" + matcher, i) or expr[end : end + 1] not in {",", "}"}:
                    raise ScopeError("selector without the tenant matcher")
                out.append("{")
            else:
                glue = "" if _next_significant(expr, i + 1) == "}" else ","
                out.append("{" + matcher + glue)
            pending_labels = False
            in_block = True
            i += 1
            continue
        if c == "}":
            in_block = False
        elif c == "[":
            bracket += 1
        elif c == "]":
            bracket = max(0, bracket - 1)
        if c == "(":
            if label_depth:
                label_depth += 1
            elif pending_labels:
                label_depth = 1
            pending_labels = False
            out.append(c)
            i += 1
            continue
        if c == ")" and label_depth:
            label_depth -= 1
        if c.isdigit() or (c == "." and i + 1 < n and expr[i + 1].isdigit()):
            m = _NUMBER.match(expr, i)
            end = m.end() if m else i + 1
            out.append(expr[i:end])
            i = end
            pending_labels = False
            continue
        m = _IDENT.match(expr, i)
        if m is None:
            out.append(c)
            pending_labels = False
            i += 1
            continue
        word = m.group()
        low = word.lower()
        i = m.end()
        pending_labels = False
        if not promql or label_depth or in_block or bracket:
            out.append(word)
            continue
        if low in _LABEL_KEYWORDS:
            # label names follow only when a parenthesis does; `group_left` may stand alone
            pending_labels = _next_significant(expr, i) == "("
            out.append(word)
            continue
        if low in _OTHER_KEYWORDS:
            out.append(word)
            continue
        nxt = _next_significant(expr, i)
        if nxt in {"(", "{"}:
            out.append(word)
            continue
        if low in _AGGREGATIONS and _next_word(expr, i) in {"by", "without"}:
            out.append(word)
            continue
        if check:
            raise ScopeError("metric name without the tenant matcher")
        out.append(word + "{" + matcher + "}")
    return "".join(out)


def _scope(expr: str, tenant_id: UUID, *, promql: bool) -> str:
    matcher = tenant_matcher(tenant_id)
    rewritten = _scan(expr, matcher, promql=promql, check=False)
    _scan(rewritten, matcher, promql=promql, check=True)
    return rewritten


def scope_promql(expr: str, tenant_id: UUID) -> str:
    """Return the PromQL expression with ``tenant="<id>"`` as the first matcher of every selector.

    Raises:
        ScopeError: If the expression cannot be scoped safely.

    Example:
        >>> from uuid import UUID
        >>> t = UUID("11111111-1111-1111-1111-111111111111")
        >>> scope_promql('up{job="x"} / on(job) group_left() other', t).count("tenant=")
        2
    """
    return _scope(expr, tenant_id, promql=True)


def scope_logql(expr: str, tenant_id: UUID) -> str:
    """Return the LogQL expression with ``tenant="<id>"`` first in every stream selector.

    Raises:
        ScopeError: If the expression cannot be scoped safely.
    """
    return _scope(expr, tenant_id, promql=False)


# -- requests -------------------------------------------------------------------------------------

Params = list[tuple[str, str]]

_LABEL_NAME = re.compile(r"^(?:__name__|[A-Za-z_][A-Za-z0-9_]{0,127})$")

#: Prometheus endpoints (below ``/api/v1/``) and what they carry: ``query`` parameters to rewrite,
#: ``match[]`` selectors to rewrite, and whether a ``match[]`` must be added when absent.
_PROM_QUERY = frozenset({"query", "query_range", "query_exemplars"})
_PROM_MATCH = frozenset({"series"})
_PROM_LABELS = frozenset({"labels"})
_PROM_PASS = frozenset({"status/buildinfo"})

_LOKI_QUERY = frozenset(
    {"query", "query_range", "index/stats", "index/volume", "index/volume_range", "patterns"}
)
_LOKI_MATCH = frozenset({"series"})
_LOKI_LABELS = frozenset({"labels"})


def scope_prometheus_request(path: str, params: Params, tenant_id: UUID) -> Params:
    """Scope a Prometheus HTTP API call; ``path`` is relative to ``/api/v1/``.

    Returns the parameters to send (all of them, from the URL and the body together, so the upstream
    never sees a second, unscoped copy).

    Raises:
        ScopeError: If the endpoint is not allowed or a query cannot be scoped.

    Example:
        >>> from uuid import UUID
        >>> t = UUID("11111111-1111-1111-1111-111111111111")
        >>> scope_prometheus_request("labels", [], t)[0][0]
        'match[]'
        >>> scope_prometheus_request("status/config", [], t)
        Traceback (most recent call last):
            ...
        switchyard.domain.query_scope.ScopeError: Prometheus endpoint 'status/config' is not allowed
    """
    return _scope_request(
        "Prometheus",
        path,
        params,
        tenant_id,
        promql=True,
        queries=_PROM_QUERY,
        matches=_PROM_MATCH,
        labels=_PROM_LABELS,
        passthrough=_PROM_PASS,
        label_values=True,
        add_key="match[]",
    )


def scope_loki_request(path: str, params: Params, tenant_id: UUID) -> Params:
    """Scope a Loki HTTP API call; ``path`` is relative to ``/loki/api/v1/``.

    Raises:
        ScopeError: If the endpoint is not allowed (including ``tail``, which is a WebSocket) or a
            query cannot be scoped.
    """
    return _scope_request(
        "Loki",
        path,
        params,
        tenant_id,
        promql=False,
        queries=_LOKI_QUERY,
        matches=_LOKI_MATCH,
        labels=_LOKI_LABELS,
        passthrough=frozenset(),
        label_values=True,
        add_key="query",
    )


def _scope_request(
    product: str,
    path: str,
    params: Params,
    tenant_id: UUID,
    *,
    promql: bool,
    queries: frozenset[str],
    matches: frozenset[str],
    labels: frozenset[str],
    passthrough: frozenset[str],
    label_values: bool,
    add_key: str,
) -> Params:
    selector = "{" + tenant_matcher(tenant_id) + "}"
    path = path.strip("/")
    scope = scope_promql if promql else scope_logql

    def each(key: str) -> Params:
        return [(k, scope(v, tenant_id) if k == key else v) for k, v in params]

    if path in passthrough:
        return list(params)
    if path in queries:
        if not any(k == "query" for k, _ in params):
            raise ScopeError("the request has no query")
        return each("query")
    if path in matches:
        key = "match[]"
        if not any(k == key for k, _ in params):
            raise ScopeError("the request has no match[] selector")
        return each(key)
    is_values = False
    if label_values:
        parts = path.split("/")
        is_values = len(parts) == 3 and parts[0] == "label" and parts[2] == "values"
        if is_values and not _LABEL_NAME.match(parts[1]):
            raise ScopeError("invalid label name")
    if path in labels or is_values:
        if any(k == add_key for k, _ in params):
            return each(add_key)
        return [*params, (add_key, selector)]
    raise ScopeError(f"{product} endpoint {path!r} is not allowed")
