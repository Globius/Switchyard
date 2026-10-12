"""Tenant scoping of PromQL, LogQL and the data source APIs (ADR 0085)."""

from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from switchyard.domain.query_scope import (
    ScopeError,
    scope_logql,
    scope_loki_request,
    scope_prometheus_request,
    scope_promql,
)

T = UUID("11111111-1111-1111-1111-111111111111")
M = f'tenant="{T}"'


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("up", f"up{{{M}}}"),
        ("up{job='x'}", f"up{{{M},job='x'}}"),
        ("up{}", f"up{{{M}}}"),
        ("{__name__=~'.+'}", f"{{{M},__name__=~'.+'}}"),
        ("rate(foo[5m])", f"rate(foo{{{M}}}[5m])"),
        ("sum by (a, b) (foo)", f"sum by (a, b) (foo{{{M}}})"),
        ("sum(foo) by (a)", f"sum(foo{{{M}}}) by (a)"),
        ("sum without(a) (foo)", f"sum without(a) (foo{{{M}}})"),
        ("a / on(x) group_left(y) b", f"a{{{M}}} / on(x) group_left(y) b{{{M}}}"),
        ("a * on(x) group_left b", f"a{{{M}}} * on(x) group_left b{{{M}}}"),
        ("a and ignoring(x) b", f"a{{{M}}} and ignoring(x) b{{{M}}}"),
        ("foo offset 5m", f"foo{{{M}}} offset 5m"),
        ("foo @ 100", f"foo{{{M}}} @ 100"),
        ("foo @ end()", f"foo{{{M}}} @ end()"),
        ("max_over_time(rate(foo[1m])[1h:5m])", f"max_over_time(rate(foo{{{M}}}[1m])[1h:5m])"),
        ("topk(3, foo)", f"topk(3, foo{{{M}}})"),
        (
            'label_replace(foo, "a", "$1", "b", "(.*)")',
            f'label_replace(foo{{{M}}}, "a", "$1", "b", "(.*)")',
        ),
        ('foo{a="}"}', f'foo{{{M},a="}}"}}'),
        ("foo # up{x}\n + bar", f"foo{{{M}}} # up{{x}}\n + bar{{{M}}}"),
        ("1 + 1", "1 + 1"),
        ("vector(1)", "vector(1)"),
        ("foo{tenant=~'.*'}", f"foo{{{M},tenant=~'.*'}}"),
        ("1e-3 * foo", f"1e-3 * foo{{{M}}}"),
        ("Inf + foo", f"Inf + foo{{{M}}}"),
    ],
)
def test_promql(expr: str, expected: str) -> None:
    assert scope_promql(expr, T) == expected


@pytest.mark.parametrize("expr", ['up{a="b', "foo`bar", "upé", "up\x00"])
def test_promql_fails_closed(expr: str) -> None:
    with pytest.raises(ScopeError):
        scope_promql(expr, T)


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ('{frontend="vs_a"}', f'{{{M},frontend="vs_a"}}'),
        ("{}", f"{{{M}}}"),
        (
            'sum by (frontend) (rate({frontend="a"} |= "x" | json [1m]))',
            f'sum by (frontend) (rate({{{M},frontend="a"}} |= "x" | json [1m]))',
        ),
        (
            '{a="1"} | line_format "{{.x}}"',
            f'{{{M},a="1"}} | line_format "{{{{.x}}}}"',
        ),
        ("{a=`}`}", f"{{{M},a=`}}`}}"),
        (
            'sum(rate({a="1"}[1m])) / sum(rate({b="2"}[1m]))',
            f'sum(rate({{{M},a="1"}}[1m])) / sum(rate({{{M},b="2"}}[1m]))',
        ),
    ],
)
def test_logql(expr: str, expected: str) -> None:
    assert scope_logql(expr, T) == expected


def test_logql_has_no_single_quotes() -> None:
    with pytest.raises(ScopeError):
        scope_logql("{a='}'} ", T)


_NAMES = st.sampled_from(["up", "foo", "haproxy_x", "a:b", "x_total"])
_LABELS = st.sampled_from(["a", "b", "job", "tenant", "proxy"])


@st.composite
def _expr(draw: st.DrawFn, depth: int = 0) -> tuple[str, int]:
    """A random PromQL expression and how many selectors it holds."""
    kinds = ["bare", "block"]
    if depth <= 3:
        kinds += ["func", "agg_before", "agg_after", "bin", "paren", "offset", "sub"]
    kind = draw(st.sampled_from(kinds))
    if kind == "bare":
        return draw(_NAMES), 1
    if kind == "block":
        label = draw(_LABELS)
        op = draw(st.sampled_from(["=", "!=", "=~", "!~"]))
        value = draw(st.sampled_from(['"x"', '"}"', "'a{b'", '".*"', '"\\""']))
        name = draw(st.sampled_from(["", draw(_NAMES)]))
        return f"{name}{{{label}{op}{value}}}", 1
    inner, n = draw(_expr(depth + 1))
    if kind == "func":
        return f"rate({inner}[5m])", n
    if kind == "agg_before":
        agg = draw(st.sampled_from(["sum", "avg", "max", "count"]))
        word = draw(st.sampled_from(["by", "without"]))
        return f"{agg} {word} ({draw(_LABELS)}, {draw(_LABELS)}) ({inner})", n
    if kind == "agg_after":
        return f"sum({inner}) by ({draw(_LABELS)})", n
    if kind == "paren":
        return f"({inner})", n
    if kind == "offset":
        return f"({inner}) offset 5m", n
    if kind == "sub":
        return f"max_over_time(({inner})[1h:5m])", n
    other, m = draw(_expr(depth + 1))
    op = draw(st.sampled_from(["+", "/", "and", "or", "unless", "* on(a) group_left(b)", "> bool"]))
    return f"({inner}) {op} ({other})", n + m


@given(_expr())
def test_every_selector_is_scoped(sample: tuple[str, int]) -> None:
    expr, selectors = sample
    out = scope_promql(expr, T)
    # exactly one tenant matcher per selector, and it is the first matcher of its block
    assert out.count(f"{{{M}") == selectors


def test_prometheus_endpoints() -> None:
    assert scope_prometheus_request("query_range", [("query", "up"), ("step", "15")], T) == [
        ("query", f"up{{{M}}}"),
        ("step", "15"),
    ]
    assert scope_prometheus_request("series", [("match[]", "up"), ("match[]", '{job="a"}')], T) == [
        ("match[]", f"up{{{M}}}"),
        ("match[]", f'{{{M},job="a"}}'),
    ]
    assert scope_prometheus_request("labels", [], T) == [("match[]", f"{{{M}}}")]
    assert scope_prometheus_request("label/tenant/values", [], T) == [("match[]", f"{{{M}}}")]
    assert scope_prometheus_request("label/proxy/values", [("match[]", "up")], T) == [
        ("match[]", f"up{{{M}}}")
    ]
    assert scope_prometheus_request("status/buildinfo", [], T) == []


@pytest.mark.parametrize(
    "path",
    [
        "status/config",
        "targets",
        "rules",
        "alerts",
        "admin/tsdb/delete_series",
        "metadata",
        "label/../../status/config/values",
        "label/a b/values",
        "write",
        "federate",
        "",
    ],
)
def test_prometheus_refuses_other_endpoints(path: str) -> None:
    with pytest.raises(ScopeError):
        scope_prometheus_request(path, [("query", "up")], T)


def test_query_endpoints_need_a_query() -> None:
    with pytest.raises(ScopeError):
        scope_prometheus_request("query", [], T)
    with pytest.raises(ScopeError):
        scope_prometheus_request("series", [], T)


def test_loki_endpoints() -> None:
    assert scope_loki_request("query_range", [("query", '{frontend="a"}')], T) == [
        ("query", f'{{{M},frontend="a"}}')
    ]
    assert scope_loki_request("labels", [], T) == [("query", f"{{{M}}}")]
    assert scope_loki_request("label/frontend/values", [("query", '{frontend="a"}')], T) == [
        ("query", f'{{{M},frontend="a"}}')
    ]
    assert scope_loki_request("series", [("match[]", '{a="b"}')], T) == [
        ("match[]", f'{{{M},a="b"}}')
    ]


@pytest.mark.parametrize("path", ["tail", "push", "rules", "delete", "format_query"])
def test_loki_refuses_other_endpoints(path: str) -> None:
    with pytest.raises(ScopeError):
        scope_loki_request(path, [("query", '{a="b"}')], T)
