# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Release versions and compatibility between controller, agents and the database (ADR 0205).

Pure functions and models, no I/O. The check *warns and recommends*; it never refuses an old
engine, because an engine must keep serving. ``severity`` is ``info``, ``warning`` or ``error``.

Example:
    >>> compat = Compat(min_agent="0.5.0", recommended_min_agent="0.9.0")
    >>> [f.code for f in check_agent("0.4.0", "0.10.0", compat)]
    ['agent_unsupported', 'agent_behind']
    >>> check_agent("0.10.0", "0.10.0", compat)
    []
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Severity = Literal["info", "warning", "error"]

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Parse `MAJOR.MINOR.PATCH` (a leading `v` and a `-pre` or `+build` suffix are ignored).

    Returns ``None`` for anything else (for example the agent's ``dev`` build), so callers decide
    what an unknown version means.

    Example:
        >>> parse_version("v1.2.3-rc1")
        (1, 2, 3)
        >>> parse_version("dev") is None
        True
    """
    m = _VERSION.match(text.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


@dataclass(frozen=True)
class Compat:
    """What this controller release supports.

    Attributes:
        min_agent: Oldest agent the controller still pushes to; older ones get an ``error`` finding
            (but are still pushed to, with the oldest bundle format).
        recommended_min_agent: Oldest agent that is fully supported; older ones get a ``warning``.
    """

    min_agent: str
    recommended_min_agent: str


@dataclass(frozen=True)
class Finding:
    """One result of an upgrade check: a stable ``code``, how bad it is, and what to do."""

    code: str
    severity: Severity
    subject: str
    message: str
    action: str = ""


def check_agent(
    agent_version: str, latest: str, compat: Compat, subject: str = ""
) -> list[Finding]:
    """Findings for one engine's agent version. Never raises; unknown versions are reported.

    Args:
        agent_version: The version the agent reported (may be ``dev`` or empty).
        latest: The newest release known (the running controller or the release feed).
        compat: This controller's compatibility table.
        subject: Engine name, for the finding.
    """
    have = parse_version(agent_version)
    if have is None:
        return [
            Finding(
                "agent_version_unknown",
                "warning",
                subject,
                f"Agent version {agent_version or 'not reported'!r} cannot be compared.",
                "Replace the engine from a current template.",
            )
        ]
    out: list[Finding] = []
    minimum = parse_version(compat.min_agent)
    recommended = parse_version(compat.recommended_min_agent)
    newest = parse_version(latest)
    if minimum and have < minimum:
        out.append(
            Finding(
                "agent_unsupported",
                "error",
                subject,
                f"Agent {agent_version} is older than the oldest supported ({compat.min_agent}); "
                "it still receives config.",
                "Roll the engine to the latest template.",
            )
        )
    if (recommended and have < recommended) or (newest and have < newest):
        out.append(
            Finding(
                "agent_behind",
                "warning",
                subject,
                f"Agent {agent_version} is behind {latest}.",
                "Upgrade the engine to the latest template (runbooks/rolling-upgrade.md).",
            )
        )
    return out


def check_schema(current: str | None, head: str) -> list[Finding]:
    """Compare the database's Alembic revision to this code's head.

    `None` means no `alembic_version` row (an empty database). A database at a revision other than
    ``head`` is *pending* when the code is newer; the code cannot tell newer-than-code from pending,
    so the caller passes ``known`` revisions through :func:`schema_state` when it can.
    """
    if current == head:
        return []
    return [
        Finding(
            "schema_pending",
            "warning",
            "database",
            f"Database is at {current or 'no revision'}, this release expects {head}.",
            "Run `syctl db upgrade` after a backup.",
        )
    ]


def schema_state(
    current: str | None, head: str, known: frozenset[str]
) -> Literal["current", "pending", "newer"]:
    """`newer` when the database is at a revision this code does not know.

    Example:
        >>> schema_state("0044", "0041", frozenset({"0040", "0041"}))
        'newer'
    """
    if current == head:
        return "current"
    if current is not None and current not in known:
        return "newer"
    return "pending"


def check_backup(last_ok: datetime | None, now: datetime, max_age_days: int = 7) -> list[Finding]:
    """Advise a backup before upgrading when there is none or the latest is old.

    Example:
        >>> from datetime import UTC, timedelta
        >>> now = datetime(2026, 10, 13, tzinfo=UTC)
        >>> [f.code for f in check_backup(None, now)]
        ['backup_missing']
        >>> check_backup(now - timedelta(days=1), now)
        []
    """
    if last_ok is None:
        return [
            Finding(
                "backup_missing", "warning", "backup",
                "No configuration backup has been taken.",
                "Add a backup target and run a backup before upgrading.",
            )
        ]  # fmt: skip
    age = now - last_ok
    if age > timedelta(days=max_age_days):
        return [
            Finding(
                "backup_stale", "warning", "backup",
                f"The latest backup is {age.days} days old.",
                "Run a backup before upgrading.",
            )
        ]  # fmt: skip
    return []
