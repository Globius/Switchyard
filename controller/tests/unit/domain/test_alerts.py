# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Alert targets and rules: validators, address policy, breach rule (ADR 0120, 0121)."""

from typing import Any

import pytest

from switchyard.domain.alerts import (
    METRICS,
    AlertInvalid,
    metric_breached,
    private_address,
    validate_rule,
    validate_target,
)

ACTIONS = frozenset({"change_set.failed", "drift.detected"})


@pytest.mark.parametrize(
    "host",
    [
        "10.1.2.3",
        "192.168.0.1",
        "127.0.0.1",
        "169.254.169.254",
        "::1",
        "fd00::1",
        "[fe80::1]",
        "localhost",
        "x.localhost",
        "0.0.0.0",  # noqa: S104
        "224.0.0.1",
        "100.64.0.1",
    ],
)
def test_private_and_local_addresses(host: str) -> None:
    assert private_address(host) is True


@pytest.mark.parametrize("host", ["93.184.216.34", "2606:2800:220:1::1", "hooks.example.test"])
def test_public_hosts_pass(host: str) -> None:
    assert private_address(host) is False


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://hooks.example.test/x", "scheme"),
        ("ftp://hooks.example.test/x", "scheme"),
        ("https://user:pw@hooks.example.test/x", "credentials"),
        ("https://10.0.0.5/x", "private_address"),
        ("https://169.254.169.254/latest/meta-data", "private_address"),
        ("https://[::1]/x", "private_address"),
        ("https://localhost/x", "private_address"),
        ("https:///nohost", "invalid"),
        ("https://a.example.test/x y", "invalid"),
        ("", "required"),
    ],
)
def test_webhook_urls_are_refused(url: str, code: str) -> None:
    with pytest.raises(AlertInvalid) as err:
        validate_target("webhook", {"url": url})
    assert err.value.errors[0]["code"] == code


def test_the_operator_can_allow_http_and_private_addresses_for_a_lab() -> None:
    assert validate_target(
        "webhook", {"url": "http://10.0.0.5:8080/hook"}, allow_http=True, allow_private=True
    ) == {"url": "http://10.0.0.5:8080/hook"}


def test_chat_and_email_targets() -> None:
    assert validate_target("chat", {"url": "https://h.example.test/x"}) == {
        "url": "https://h.example.test/x",
        "format": "slack",
    }
    with pytest.raises(AlertInvalid) as err:
        validate_target("chat", {"url": "https://h.example.test/x", "format": "irc"})
    assert err.value.errors[0]["path"] == "config.format"
    with pytest.raises(AlertInvalid) as err:
        validate_target("email", {"to": ["ok@example.test", "not-an-address", "a@b"]})
    assert [e["path"] for e in err.value.errors] == ["config.to[1]", "config.to[2]"]
    with pytest.raises(AlertInvalid):
        validate_target("email", {"to": []})
    with pytest.raises(AlertInvalid) as err:
        validate_target("email", {"to": ["a@example.test"], "url": "https://x.example.test"})
    assert err.value.errors[0]["code"] == "unknown_field"
    with pytest.raises(AlertInvalid):
        validate_target("pager", {})


def test_event_rules_name_known_actions_and_known_targets() -> None:
    spec, names = validate_rule(
        "event",
        {"actions": ["drift.detected", "change_set.failed", "drift.detected"]},
        ["ops", "ops"],
        {"ops"},
        ACTIONS,
    )
    assert spec == {"actions": ["change_set.failed", "drift.detected"]} and names == ["ops"]
    with pytest.raises(AlertInvalid) as err:
        validate_rule("event", {"actions": ["auth.login"]}, ["nobody"], {"ops"}, ACTIONS)
    assert {e["path"] for e in err.value.errors} == {"spec.actions[0]", "targets[0]"}


def test_metric_rules_choose_from_a_fixed_list_and_never_take_promql() -> None:
    ok, _ = validate_rule(
        "metric",
        {"metric": "engine_cpu_percent", "op": "gt", "threshold": 90, "scope": "se-a"},
        ["ops"],
        {"ops"},
    )
    assert ok == {
        "metric": "engine_cpu_percent",
        "op": "gt",
        "threshold": 90,
        "for_minutes": 5,
        "scope": "se-a",
    }
    bad: list[dict[str, Any]] = [
        {"metric": "up{job='x'}", "op": "gt", "threshold": 1},
        {"metric": "vs_rps", "op": "eq", "threshold": 1},
        {"metric": "vs_rps", "op": "gt", "threshold": "high"},
        {"metric": "vs_rps", "op": "gt", "threshold": True},
        {"metric": "vs_rps", "op": "gt", "threshold": 1, "for_minutes": -1},
        {"metric": "vs_rps", "op": "gt", "threshold": 1, "for_minutes": 2000},
        {"metric": "vs_rps", "op": "gt", "threshold": 1, "scope": 'a"}or{x="'},
        {"metric": "vs_rps", "op": "gt", "threshold": 1, "expr": "up"},
    ]
    for spec in bad:
        with pytest.raises(AlertInvalid):
            validate_rule("metric", spec, ["ops"], {"ops"})
    assert set(METRICS) >= {"vs_5xx_ratio", "engine_cpu_percent"}


def test_certificate_rules_default_to_30_14_7_days() -> None:
    assert validate_rule("cert_expiry", {"days": [7, 30, 14, 30]}, ["a"], {"a"})[0] == {
        "days": [30, 14, 7]
    }
    for days in ([], [0], [366], ["7"], [True], list(range(1, 9))):
        with pytest.raises(AlertInvalid):
            validate_rule("cert_expiry", {"days": days}, ["a"], {"a"})


def test_a_rule_needs_one_to_ten_targets() -> None:
    with pytest.raises(AlertInvalid):
        validate_rule("cert_expiry", {}, [], set())
    names = [f"t{i}" for i in range(11)]
    with pytest.raises(AlertInvalid):
        validate_rule("cert_expiry", {}, names, set(names))


def test_breach_direction() -> None:
    assert metric_breached("gt", 6, 5) and not metric_breached("gt", 5, 5)
    assert metric_breached("lt", 4, 5) and not metric_breached("lt", 5, 5)
