# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The upgrade check: controller, database and engine versions against the compatibility table.

See ADR 0205.
Read-only. It never blocks anything; findings carry a severity and an action. System administrators
only (permission ``manage controller``).
"""

from dataclasses import asdict, dataclass, field
from typing import Any

from alembic.script import ScriptDirectory
from sqlalchemy import text

from switchyard import __version__
from switchyard.db.migrate import alembic_config
from switchyard.db.ops_models import EngineStatusRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.versions import Compat, Finding, check_agent, schema_state
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal

#: What this release supports. Raise ``recommended_min_agent`` when a release makes older agents
#: second-class; raise ``min_agent`` only when a bundle format is dropped (ADR 0205 section 2).
COMPAT = Compat(min_agent="0.1.0", recommended_min_agent="0.1.0")


@dataclass(frozen=True)
class EngineVersion:
    """One engine's reported agent version."""

    engine_id: str
    name: str
    tenant_id: str
    agent_version: str | None


@dataclass(frozen=True)
class UpgradeCheck:
    """The result of :meth:`UpgradeService.check`."""

    controller_version: str
    latest_version: str
    schema_current: str | None
    schema_head: str
    schema_state: str
    engines: list[EngineVersion] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready form."""
        return asdict(self)


class UpgradeService:
    """Builds the upgrade check from the database and the stored engine status."""

    def __init__(self, db: Databases, latest_version: str | None = None) -> None:
        """``latest_version`` overrides the newest known release (the release feed supplies it)."""
        self._db = db
        self._latest = latest_version

    async def check(self, principal: Principal) -> UpgradeCheck:
        """Compare the controller, its schema and every engine's agent with the newest release.

        ``latest`` is the running controller until a release feed reports a newer one.
        """
        require(principal, "manage", "controller")
        latest = self._latest or __version__
        script = ScriptDirectory.from_config(alembic_config("postgresql+psycopg://x"))
        head = script.get_current_head() or ""
        known = frozenset(r.revision for r in script.walk_revisions())
        async with admin_session(self._db.admin) as session:
            current = (
                await session.execute(text("select version_num from alembic_version"))
            ).scalar()
            rows = list((await session.execute(_engine_query())).all())
        engines = [
            EngineVersion(str(r[0]), r[1], str(r[2]), (r[3] or {}).get("version") if r[3] else None)
            for r in rows
        ]
        state = schema_state(current, head, known)
        findings: list[Finding] = []
        if state == "newer":
            findings.append(
                Finding(
                    "schema_newer_than_code",
                    "error",
                    "database",
                    f"Database is at {current}, which this release does not know.",
                    "Do not run this release against it; upgrade the controller.",
                )
            )
        elif state == "pending":
            findings.append(
                Finding(
                    "schema_pending",
                    "warning",
                    "database",
                    f"Database is at {current}, this release expects {head}.",
                    "Back up, then run `syctl db upgrade`.",
                )
            )
        for e in engines:
            findings.extend(check_agent(e.agent_version or "", latest, COMPAT, e.name))
        return UpgradeCheck(__version__, latest, current, head, state, engines, findings)


def _engine_query() -> Any:
    from sqlalchemy import select

    return select(
        EngineStatusRow.engine_id,
        EngineStatusRow.name,
        EngineStatusRow.tenant_id,
        EngineStatusRow.agent,
    )
