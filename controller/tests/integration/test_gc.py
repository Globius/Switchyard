"""Housekeeping: expires abandoned drafts, old jobs and stale credentials; keeps everything else."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from switchyard.db.tenancy import admin_session
from switchyard.jobs import queue
from switchyard.services.gc import GarbageCollector
from switchyard.services.status import StatusService
from tests.pipeline_env import TENANT, World, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")


async def scalar(w: World, sql: str) -> int:
    async with admin_session(w.db.admin) as s:
        return int((await s.execute(text(sql))).scalar_one())


async def seed(w: World) -> None:
    """One draft, one finished job, one queued job, one status row, expired login material."""
    await w.service.open(principal(), TENANT, "abandoned")
    async with admin_session(w.db.admin) as s:
        done = await queue.enqueue(s, "poll_status", {})
        await queue.enqueue(s, "poll_status", {})
        await s.execute(
            text("UPDATE job SET state='succeeded', finished_at=now() WHERE id=:i"), {"i": done}
        )
        await s.execute(
            text(
                "INSERT INTO identity_provider (id, name, issuer_url, client_id) "
                "VALUES (gen_random_uuid(), 'idp', 'https://idp.example.test', 'c')"
            )
        )
        await s.execute(
            text(
                "INSERT INTO login_state (state, provider_id, nonce, code_verifier, expires_at) "
                "SELECT 's1', id, 'n', 'v', now() - interval '1 hour' FROM identity_provider"
            )
        )
        await s.execute(
            text(
                "INSERT INTO user_session (id, token_hash, csrf_token, subject, name, expires_at) "
                "VALUES (gen_random_uuid(), 'h1', 'c', 'x', 'x', now() - interval '1 hour')"
            )
        )
    await StatusService(w.db, w.pipeline._directory).poll_all()


async def test_nothing_recent_is_removed(pg_url: str) -> None:
    async with world(pg_url) as w:
        await seed(w)
        done = await GarbageCollector(w.db).run()
        assert done["drafts_discarded"] == 0 and done["jobs_deleted"] == 0
        assert done["sessions_deleted"] == 0, "expired sessions are kept for a week"
        assert done["login_states_deleted"] == 1, "expired login attempts go at once"
        assert await scalar(w, "SELECT count(*) FROM job") == 2
        assert await scalar(w, "SELECT count(*) FROM change_set WHERE state='draft'") == 1


async def test_old_things_are_expired_and_the_rest_is_untouched(pg_url: str) -> None:
    async with world(pg_url) as w:
        await seed(w)
        later = datetime.now(UTC) + timedelta(days=40)
        done = await GarbageCollector(w.db, clock=lambda: later).run()
        assert done == {
            "drafts_discarded": 1,
            "jobs_deleted": 1,
            "status_rows_deleted": 2,
            "login_states_deleted": 1,
            "login_throttle_deleted": 0,
            "grafana_grants_deleted": 0,
            "sessions_deleted": 1,
            "enrollment_tokens_deleted": 0,
        }
        assert await scalar(w, "SELECT count(*) FROM job WHERE state='queued'") == 1
        assert await scalar(w, "SELECT count(*) FROM change_set WHERE state='discarded'") == 1
        assert await scalar(w, "SELECT count(*) FROM tenant") == 1
        assert await scalar(w, "SELECT count(*) FROM audit_event WHERE action='gc.run'") == 1
        again = await GarbageCollector(w.db, clock=lambda: later).run()
        assert not any(again.values())
        assert await scalar(w, "SELECT count(*) FROM audit_event WHERE action='gc.run'") == 1
