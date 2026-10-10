"""Job model: stages with timing, list and filters, log, retry and cancel (ticket T5)."""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from switchyard.adapters.fake_haproxy import FakeHaproxy
from switchyard.api.job_models import Job
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import JobState, Role
from switchyard.jobs import queue
from switchyard.jobs.handlers import build_handlers
from switchyard.jobs.worker import Worker
from switchyard.services.errors import ConflictError, NotFoundError, PermissionDeniedError
from switchyard.services.jobs import JobService
from tests.pipeline_env import TENANT, World, add_host_route, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")

STAGES = ["lock", "render", "validate", "canary", "rollout", "commit"]


async def apply_job(w: World, host: str = "new.example.test") -> uuid.UUID:
    who = principal()
    detail = await w.service.open(who, TENANT, "add a route")
    await w.service.add_ops(who, TENANT, detail.record.id, [add_host_route(host)])
    return await w.service.apply(who, TENANT, detail.record.id, request_id="req-1")


def worker(w: World) -> Worker:
    return Worker(w.db.admin, build_handlers(w.db, w.pipeline, None), worker_id="t")


async def boom(job: queue.Job) -> dict[str, Any]:
    raise RuntimeError("nope")


async def scalar(w: World, sql: str) -> object:
    async with admin_session(w.db.admin) as s:
        return (await s.execute(text(sql))).scalar_one()


async def audit_actions(w: World) -> set[str]:
    async with admin_session(w.db.admin) as s:
        return set((await s.execute(text("SELECT action FROM audit_event"))).scalars())


async def test_applied_job_exposes_every_stage_with_timing(pg_url: str) -> None:
    async with world(pg_url) as w:
        job_id = await apply_job(w)
        queued = Job.from_row(await JobService(w.db).get(principal(), TENANT, job_id))
        assert (queued.state, queued.stages, queued.started_at) == (JobState.QUEUED, [], None)

        assert await worker(w).run_once()
        job = Job.from_row(await JobService(w.db).get(principal(), TENANT, job_id))
        assert job.state == JobState.SUCCEEDED and job.ok is True
        assert job.started_at is not None and job.finished_at is not None
        assert job.change_set_id is not None
        assert [s.name for s in job.stages] == STAGES
        for stage in job.stages:
            assert stage.state == "succeeded", stage
            assert stage.started_at and stage.finished_at and stage.error is None


async def test_log_lists_attempts_and_stages(pg_url: str) -> None:
    async with world(pg_url) as w:
        job_id = await apply_job(w)
        await worker(w).run_once()
        row = await JobService(w.db).get(principal(), TENANT, job_id)
        messages = [line["message"] for line in row.log]
        assert messages[:2] == ["attempt 1 claimed by t", "render: started"]
        assert "commit: done" in messages


async def test_failed_canary_shows_the_failing_stage_and_error(pg_url: str) -> None:
    async with world(pg_url) as w:
        canary = w.engines["se-a"]
        original = canary.files["haproxy/haproxy.cfg"]

        async def degrade(engine: FakeHaproxy) -> None:
            if engine.files["haproxy/haproxy.cfg"] != original:
                engine.set_status("pool_web", "10-20-0-11-8080", "DOWN")

        canary.on_push = degrade
        job_id = await apply_job(w)
        await worker(w).run_once()

        job = Job.from_row(await JobService(w.db).get(principal(), TENANT, job_id))
        by_name = {s.name: s for s in job.stages}
        # Finished, not retrying: the apply was reverted.
        assert (job.state, job.ok, job.retrying, job.attempts) == (
            JobState.SUCCEEDED,
            False,
            False,
            1,
        )
        assert [by_name[n].state for n in STAGES] == [
            "succeeded",
            "succeeded",
            "succeeded",
            "failed",
            "skipped",
            "skipped",
        ]
        assert "went from up to down" in (by_name["canary"].error or "")


async def test_engine_rejection_is_reported_on_the_validate_stage(pg_url: str) -> None:
    async with world(pg_url) as w:
        w.engines["se-a"].fail_next("rejected")
        job_id = await apply_job(w)
        await worker(w).run_once()
        job = Job.from_row(await JobService(w.db).get(principal(), TENANT, job_id))
        failed = [s for s in job.stages if s.state == "failed"]
        assert [s.name for s in failed] == ["validate"]
        assert "rejected" in (failed[0].error or "")


async def test_list_filters_and_pages_newest_first(pg_url: str) -> None:
    async with world(pg_url) as w:
        ids = []
        for i in range(5):  # one transaction each, so created_at differs
            async with admin_session(w.db.admin) as s:
                ids.append(
                    await queue.enqueue(s, "noop" if i % 2 else "other", {}, tenant_id=TENANT)
                )
        async with admin_session(w.db.admin) as s:
            await queue.enqueue(s, "noop", {})  # system job: in no tenant's list
        svc = JobService(w.db)
        first = await svc.list(principal(), TENANT, limit=2)
        assert [j.id for j in first.items] == [ids[4], ids[3]]
        assert first.next_cursor is not None
        second = await svc.list(principal(), TENANT, limit=2, cursor=first.next_cursor)
        assert [j.id for j in second.items] == [ids[2], ids[1]]
        last = await svc.list(principal(), TENANT, limit=2, cursor=second.next_cursor)
        assert ([j.id for j in last.items], last.next_cursor) == ([ids[0]], None)
        only = await svc.list(principal(), TENANT, kind="noop")
        assert {j.id for j in only.items} == {ids[1], ids[3]}
        assert (await svc.list(principal(), TENANT, state=JobState.RUNNING)).items == []


async def test_tenant_scoping(pg_url: str) -> None:
    async with world(pg_url) as w:
        async with admin_session(w.db.admin) as s:
            system_job = await queue.enqueue(s, "noop", {})
        svc = JobService(w.db)
        with pytest.raises(NotFoundError):
            await svc.get(principal(), TENANT, system_job)
        with pytest.raises(PermissionDeniedError):
            await svc.list(principal(tenant=uuid.uuid4()), TENANT)


async def test_retry_a_dead_job(pg_url: str) -> None:
    async with world(pg_url) as w:
        async with admin_session(w.db.admin) as s:
            job_id = await queue.enqueue(
                s, "sync_cloudflare_ips", {}, tenant_id=TENANT, max_attempts=1
            )
        await Worker(w.db.admin, {"sync_cloudflare_ips": boom}, worker_id="t").run_once()
        svc = JobService(w.db)
        dead = await svc.get(principal(), TENANT, job_id)
        assert (dead.state, dead.attempts, dead.last_error) == ("dead", 1, "RuntimeError: nope")

        with pytest.raises(PermissionDeniedError):
            await svc.retry(principal(Role.VIEWER), TENANT, job_id)
        again = await svc.retry(principal(Role.OPERATOR), TENANT, job_id, request_id="r")
        assert (again.state, again.attempts, again.finished_at) == ("queued", 0, None)
        with pytest.raises(ConflictError):
            await svc.retry(principal(), TENANT, job_id)  # now queued
        assert "job.retried" in await audit_actions(w)


async def test_cancel_only_queued_jobs_and_free_the_change_set(pg_url: str) -> None:
    async with world(pg_url) as w:
        job_id = await apply_job(w)
        svc = JobService(w.db)
        with pytest.raises(PermissionDeniedError):
            await svc.cancel(principal(Role.VIEWER), TENANT, job_id)
        cancelled = await svc.cancel(principal(Role.OPERATOR), TENANT, job_id)
        assert cancelled.state == JobState.CANCELLED and cancelled.finished_at is not None
        assert await scalar(w, "SELECT state FROM change_set") == "failed"
        assert "job.cancelled" in await audit_actions(w)
        assert not await worker(w).run_once(), "a cancelled job is never claimed"
        with pytest.raises(ConflictError):
            await svc.cancel(principal(), TENANT, job_id)


async def test_a_failed_attempt_is_retrying_not_finished(pg_url: str) -> None:
    async with world(pg_url) as w:
        async with admin_session(w.db.admin) as s:
            job_id = await queue.enqueue(s, "sync_cloudflare_ips", {}, tenant_id=TENANT)
        await Worker(w.db.admin, {"sync_cloudflare_ips": boom}, worker_id="t").run_once()
        job = Job.from_row(await JobService(w.db).get(principal(), TENANT, job_id))
        assert (job.state, job.attempts, job.retrying, job.ok) == (JobState.QUEUED, 1, True, None)
