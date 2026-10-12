# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Hourly housekeeping (backend design, "Background jobs"): expire what nobody will use again.

Never touches desired state, revisions or the audit log. What it removes: abandoned drafts, old
finished jobs, stale engine status, and expired login and enrollment material.
"""

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update

from switchyard.db import audit_log
from switchyard.db.auth_models import (
    GrafanaGrantRow,
    LoginStateRow,
    LoginThrottleRow,
    UserSessionRow,
)
from switchyard.db.ops_models import (
    ChangeSetRow,
    EngineStatusRow,
    EnrollmentTokenRow,
    JobRow,
    ServiceEngineRow,
)
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import ChangeSetState, JobState
from switchyard.services.captures import expire as expire_captures
from switchyard.services.context import Clock, Databases, utcnow

GC_JOB = "gc"
DRAFT_MAX_AGE = timedelta(days=14)
JOB_MAX_AGE = timedelta(days=30)
STATUS_MAX_AGE = timedelta(days=1)
CREDENTIAL_GRACE = timedelta(days=7)


class GarbageCollector:
    """Runs the housekeeping rules."""

    def __init__(self, db: Databases, clock: Clock = utcnow) -> None:
        """Create the collector; ``clock`` is replaceable in tests."""
        self._db = db
        self._clock = clock

    async def run(self) -> dict[str, int]:
        """Apply every rule once and return how many rows each affected."""
        now: datetime = self._clock()
        done: dict[str, int] = {}
        async with admin_session(self._db.admin) as session:
            drafts = await session.execute(
                update(ChangeSetRow)
                .where(
                    ChangeSetRow.state == ChangeSetState.DRAFT.value,
                    ChangeSetRow.updated_at < now - DRAFT_MAX_AGE,
                )
                .values(state=ChangeSetState.DISCARDED.value, finished_at=now)
            )
            done["drafts_discarded"] = drafts.rowcount  # type: ignore[attr-defined]
            jobs = await session.execute(
                delete(JobRow).where(
                    JobRow.state.in_(
                        [JobState.SUCCEEDED.value, JobState.FAILED.value, JobState.DEAD.value]
                    ),
                    JobRow.finished_at < now - JOB_MAX_AGE,
                )
            )
            done["jobs_deleted"] = jobs.rowcount  # type: ignore[attr-defined]
            alive = select(ServiceEngineRow.id)
            status = await session.execute(
                delete(EngineStatusRow).where(
                    or_(
                        EngineStatusRow.observed_at < now - STATUS_MAX_AGE,
                        EngineStatusRow.engine_id.not_in(alive),
                    )
                )
            )
            done["status_rows_deleted"] = status.rowcount  # type: ignore[attr-defined]
            states = await session.execute(
                delete(LoginStateRow).where(LoginStateRow.expires_at < now)
            )
            done["login_states_deleted"] = states.rowcount  # type: ignore[attr-defined]
            throttle = await session.execute(
                delete(LoginThrottleRow).where(
                    LoginThrottleRow.updated_at < now - timedelta(days=1)
                )
            )
            done["login_throttle_deleted"] = throttle.rowcount  # type: ignore[attr-defined]
            grants = await session.execute(
                delete(GrafanaGrantRow).where(
                    func.coalesce(
                        GrafanaGrantRow.session_expires_at, GrafanaGrantRow.ticket_expires_at
                    )
                    < now - timedelta(days=1)
                )
            )
            done["grafana_grants_deleted"] = grants.rowcount  # type: ignore[attr-defined]
            old = now - CREDENTIAL_GRACE
            sessions = await session.execute(
                delete(UserSessionRow).where(
                    or_(
                        UserSessionRow.expires_at < old,
                        and_(
                            UserSessionRow.revoked_at.is_not(None), UserSessionRow.revoked_at < old
                        ),
                    )
                )
            )
            done["sessions_deleted"] = sessions.rowcount  # type: ignore[attr-defined]
            tokens = await session.execute(
                delete(EnrollmentTokenRow).where(
                    or_(
                        EnrollmentTokenRow.expires_at < old,
                        and_(
                            EnrollmentTokenRow.used_at.is_not(None),
                            EnrollmentTokenRow.used_at < old,
                        ),
                    )
                )
            )
            done["enrollment_tokens_deleted"] = tokens.rowcount  # type: ignore[attr-defined]
            done["captures_deleted"] = await expire_captures(self._db, now)
            if any(done.values()):
                await audit_log.record(
                    session, tenant_id=None, actor="system:gc", action="gc.run", diff=dict(done)
                )
        return done
