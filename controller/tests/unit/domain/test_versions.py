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
    for text in ("", "nightly", "1.0"):
        (f,) = check_agent(text, "0.10.0", COMPAT)
        assert (f.code, f.severity) == ("agent_version_unknown", "warning")


def test_dev_builds_and_commit_hashes_are_info_not_a_replace_warning() -> None:
    for text in ("dev", "310bf75", "310BF75A", "0123456789abcdef0123456789abcdef01234567"):
        (f,) = check_agent(text, "0.10.0", COMPAT, "edge-1")
        assert (f.code, f.severity) == ("agent_dev_build", "info")
        assert "Replace the engine" not in f.action


def test_release_stamped_with_build_metadata_compares_normally() -> None:
    assert check_agent("0.10.0+310bf75", "0.10.0", COMPAT) == []
    assert check_agent("0.9.0+310bf75", "0.10.0", COMPAT)[0].code == "agent_behind"


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


def test_version_precedence_follows_semver() -> None:
    from switchyard.domain.versions import version_key

    order = [
        "0.1.9",
        "0.2.0-nightly.9",
        "0.2.0-nightly.20261013",
        "0.2.0-nightly.20261014",
        "0.2.0-rc1",
        "0.2.0",
        "0.2.1-nightly.1",
        "0.10.0",
        "1.0.0",
    ]
    keys = [version_key(v) for v in order]
    assert None not in keys and keys == sorted(keys)  # type: ignore[type-var]
    assert version_key("v1.0.0+build5") == version_key("1.0.0")
    assert version_key("dev") is None and version_key("1.0") is None


def test_a_nightly_agent_is_behind_the_release_it_leads_to() -> None:
    from switchyard.domain.versions import check_agent

    compat = Compat(min_agent="0.1.0", recommended_min_agent="0.2.0")
    codes = [f.code for f in check_agent("0.2.0-nightly.1", "0.2.0", compat)]
    assert codes == ["agent_behind"]
    assert check_agent("0.2.0-nightly.2", "0.2.0-nightly.2", Compat("0.1.0", "0.1.0")) == []
