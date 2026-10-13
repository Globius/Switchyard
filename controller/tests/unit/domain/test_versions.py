# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the version and compatibility checks (ADR 0205)."""

from switchyard.domain.versions import (
    Compat,
    check_agent,
    check_schema,
    parse_version,
    schema_state,
)

COMPAT = Compat(min_agent="0.5.0", recommended_min_agent="0.9.0")


def test_parse_version() -> None:
    assert parse_version("1.2.3") == (1, 2, 3)
    assert parse_version("v0.10.0-rc1+b5") == (0, 10, 0)
    assert parse_version("dev") is None
    assert parse_version("") is None


def test_current_agent_has_no_findings() -> None:
    assert check_agent("0.10.0", "0.10.0", COMPAT) == []


def test_old_agent_is_flagged_but_not_denied() -> None:
    codes = [f.code for f in check_agent("0.4.0", "0.10.0", COMPAT, "se1")]
    assert codes == ["agent_unsupported", "agent_behind"]


def test_supported_but_behind_is_a_warning() -> None:
    (f,) = check_agent("0.9.0", "0.10.0", COMPAT)
    assert (f.code, f.severity) == ("agent_behind", "warning")


def test_unknown_version_is_reported() -> None:
    (f,) = check_agent("dev", "0.10.0", COMPAT)
    assert f.code == "agent_version_unknown"


def test_schema() -> None:
    assert check_schema("0041", "0041") == []
    assert check_schema(None, "0041")[0].code == "schema_pending"
    known = frozenset({"0040", "0041"})
    assert schema_state("0041", "0041", known) == "current"
    assert schema_state("0040", "0041", known) == "pending"
    assert schema_state("0044", "0041", known) == "newer"


def test_backup_age() -> None:
    from datetime import UTC, datetime, timedelta

    from switchyard.domain.versions import check_backup

    now = datetime(2026, 10, 13, tzinfo=UTC)
    assert [f.code for f in check_backup(None, now)] == ["backup_missing"]
    assert [f.code for f in check_backup(now - timedelta(days=9), now)] == ["backup_stale"]
    assert check_backup(now - timedelta(days=2), now) == []
