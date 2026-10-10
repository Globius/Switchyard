"""The PostgreSQL job queue and the worker loop, against a real database."""

import asyncio
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.db.repository import add_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.jobs import queue
from switchyard.jobs.queue import Job, backoff
from switchyard.jobs.worker import Worker


async def state_of(engine: AsyncEngine, job_id: Any) -> tuple[str, int, str | None]:
    async with admin_session(engine) as s:
        row = (
            await s.execute(
                text("SELECT state, attempts, last_error FROM job WHERE id = :i"), {"i": job_id}
            )
        ).one()
    return str(row[0]), int(row[1]), row[2]


async def enqueue(engine: AsyncEngine, kind: str = "noop", **kw: Any) -> Any:
    async with admin_session(engine) as s:
        return await queue.enqueue(s, kind, {"n": 1}, **kw)


async def test_claim_returns_the_oldest_runnable_job_once(admin_engine: AsyncEngine) -> None:
    first = await enqueue(admin_engine)
    second = await enqueue(admin_engine)
    job = await queue.claim(admin_engine, "w1")
    assert job is not None
    assert (job.id, job.attempts, job.payload) == (first, 1, {"n": 1})
    other = await queue.claim(admin_engine, "w2")
    assert other is not None
    assert other.id == second
    assert await queue.claim(admin_engine, "w3") is None


async def test_concurrent_claimers_never_get_the_same_job(admin_engine: AsyncEngine) -> None:
    for _ in range(6):
        await enqueue(admin_engine)
    results = await asyncio.gather(*(queue.claim(admin_engine, f"w{i}") for i in range(6)))
    ids = [j.id for j in results if j is not None]
    assert len(ids) == len(set(ids)) == 6


async def test_delayed_jobs_wait(admin_engine: AsyncEngine) -> None:
    await enqueue(admin_engine, delay=timedelta(hours=1))
    assert await queue.claim(admin_engine, "w1") is None


async def test_kind_filter(admin_engine: AsyncEngine) -> None:
    await enqueue(admin_engine, "other")
    assert await queue.claim(admin_engine, "w1", {"noop"}) is None
    claimed = await queue.claim(admin_engine, "w1", {"other"})
    assert claimed is not None
    assert claimed.kind == "other"


async def test_failure_requeues_with_backoff_then_dies(admin_engine: AsyncEngine) -> None:
    job_id = await enqueue(admin_engine, max_attempts=2)
    job = await queue.claim(admin_engine, "w1")
    assert job is not None
    assert await queue.fail(admin_engine, job.id, "boom", delay=timedelta(0)) == "queued"
    assert (await state_of(admin_engine, job_id))[0] == "queued"
    job = await queue.claim(admin_engine, "w1")
    assert job is not None
    assert job.attempts == 2
    assert await queue.fail(admin_engine, job.id, "boom again") == "dead"
    assert await state_of(admin_engine, job_id) == ("dead", 2, "boom again")
    assert await queue.claim(admin_engine, "w1") is None


async def test_failed_job_is_not_runnable_until_its_backoff_passes(
    admin_engine: AsyncEngine,
) -> None:
    await enqueue(admin_engine)
    job = await queue.claim(admin_engine, "w1")
    assert job is not None
    await queue.fail(admin_engine, job.id, "boom")  # default backoff: seconds in the future
    assert await queue.claim(admin_engine, "w1") is None


async def test_success_records_the_result(admin_engine: AsyncEngine) -> None:
    job_id = await enqueue(admin_engine)
    job = await queue.claim(admin_engine, "w1")
    assert job is not None
    await queue.succeed(admin_engine, job.id, {"ok": True})
    async with admin_session(admin_engine) as s:
        row = (
            await s.execute(text("SELECT state, result FROM job WHERE id = :i"), {"i": job_id})
        ).one()
    assert (row[0], row[1]) == ("succeeded", {"ok": True})


async def test_silent_workers_lose_their_jobs(admin_engine: AsyncEngine) -> None:
    job_id = await enqueue(admin_engine)
    job = await queue.claim(admin_engine, "crashed")
    assert job is not None
    assert await queue.reclaim_stale(admin_engine, timedelta(hours=1)) == 0
    async with admin_session(admin_engine) as s:
        await s.execute(
            text("UPDATE job SET heartbeat_at = now() - interval '5 minutes' WHERE id = :i"),
            {"i": job_id},
        )
    assert await queue.reclaim_stale(admin_engine, timedelta(seconds=30)) == 1
    again = await queue.claim(admin_engine, "w2")
    assert again is not None
    assert (again.id, again.attempts) == (job_id, 2)


async def test_heartbeat_keeps_a_job_alive(admin_engine: AsyncEngine) -> None:
    job_id = await enqueue(admin_engine)
    job = await queue.claim(admin_engine, "w1")
    assert job is not None
    async with admin_session(admin_engine) as s:
        await s.execute(
            text("UPDATE job SET heartbeat_at = now() - interval '5 minutes' WHERE id = :i"),
            {"i": job_id},
        )
    await queue.heartbeat(admin_engine, job.id)
    assert await queue.reclaim_stale(admin_engine, timedelta(seconds=30)) == 0


async def test_worker_runs_handlers_and_retries_failures(admin_engine: AsyncEngine) -> None:
    seen: list[Job] = []

    async def ok(job: Job) -> dict[str, Any]:
        seen.append(job)
        return {"echo": job.payload}

    calls = {"n": 0}

    async def flaky(job: Job) -> None:
        calls["n"] += 1
        raise RuntimeError("nope")

    worker = Worker(
        admin_engine, {"ok": ok, "flaky": flaky}, worker_id="w", heartbeat_interval_s=0.05
    )
    good = await enqueue(admin_engine, "ok")
    bad = await enqueue(admin_engine, "flaky", max_attempts=1)
    assert await worker.run_once()
    assert await worker.run_once()
    assert not await worker.run_once()
    assert (await state_of(admin_engine, good))[0] == "succeeded"
    assert await state_of(admin_engine, bad) == ("dead", 1, "RuntimeError: nope")
    assert [j.id for j in seen] == [good]
    assert calls["n"] == 1


async def test_worker_ignores_kinds_it_has_no_handler_for(admin_engine: AsyncEngine) -> None:
    async def handler(job: Job) -> None:
        return None

    job_id = await enqueue(admin_engine, "someone-elses")
    worker = Worker(admin_engine, {"mine": handler}, worker_id="w")
    assert not await worker.run_once()
    assert (await state_of(admin_engine, job_id))[0] == "queued"


async def test_tenants_only_see_their_own_jobs(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    mapping = {
        "tenants": [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}],
    }
    async with admin_session(admin_engine) as s:
        await add_snapshot(s, snapshot_from_mapping(mapping))
        await queue.enqueue(s, "noop", {}, tenant_id=_uuid("a"))
        await queue.enqueue(s, "noop", {}, tenant_id=_uuid("b"))
        await queue.enqueue(s, "noop", {})  # system job, no tenant
    async with tenant_session(app_engine, [_uuid("a")]) as s:
        rows = (await s.execute(text("SELECT tenant_id FROM job"))).scalars().all()
    assert rows == [_uuid("a")]


def _uuid(symbol: str) -> Any:
    import uuid

    return uuid.UUID(symbolic_uuid(symbol))


def test_backoff_is_capped() -> None:
    assert backoff(50) == timedelta(seconds=300)
