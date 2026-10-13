# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Status polling, drift detection, the scheduler and the status API."""

import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import text

from switchyard.adapters.secrets import MemorySecrets
from switchyard.db.tenancy import admin_session
from switchyard.jobs.handlers import POLL_STATUS_JOB, build_handlers
from switchyard.jobs.scheduler import Scheduler
from switchyard.jobs.worker import Worker
from switchyard.services.status import StatusService
from tests.integration.test_pipeline_routing import healthy_vrrp, prime_bundles, vrrp_data
from tests.pipeline_env import CERT_SECRETS, TENANT, World, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")


def service(w: World) -> StatusService:
    return StatusService(w.db, w.pipeline._directory, MemorySecrets(CERT_SECRETS))


async def scalar(w: World, sql: str) -> object:
    async with admin_session(w.db.admin) as s:
        return (await s.execute(text(sql))).scalar_one()


async def test_poll_stores_status_and_reports_no_drift_for_a_deployed_group(pg_url: str) -> None:
    async with world(pg_url) as w:
        svc = service(w)
        assert await svc.poll_all() == 2
        rows = await svc.list_status(principal(), TENANT)
        assert [r.name for r in rows] == ["se-a", "se-b"]
        assert all(r.reachable and not r.drift for r in rows)
        assert rows[0].haproxy_fingerprint


async def test_manual_change_is_reported_as_drift_and_never_corrected(pg_url: str) -> None:
    async with world(pg_url) as w:
        svc = service(w)
        await svc.poll_all()
        hotfix = dict(w.engines["se-a"].files)
        # A config from some other render: same marker line, different digest, plus a hand edit.
        hotfix["haproxy/haproxy.cfg"] = (
            re.sub(r"(sha256:)[0-9a-f]{4}", r"\g<1>dead", hotfix["haproxy/haproxy.cfg"])
            + "# hotfix\n"
        )
        await w.engines["se-a"].push(hotfix)
        await svc.poll_all()
        rows = {r.name: r for r in await svc.list_status(principal(), TENANT)}
        assert [d["kind"] for d in rows["se-a"].drift] == ["haproxy_config"]
        assert rows["se-b"].drift == []
        assert w.engines["se-a"].files["haproxy/haproxy.cfg"].endswith("# hotfix\n")
        assert (
            await scalar(w, "SELECT count(*) FROM audit_event WHERE action='drift.detected'") == 1
        )
        await svc.poll_all()  # the same drift is not announced again
        assert (
            await scalar(w, "SELECT count(*) FROM audit_event WHERE action='drift.detected'") == 1
        )
        await w.engines["se-a"].push(w.engines["se-b"].files)
        await svc.poll_all()
        assert (
            await scalar(w, "SELECT count(*) FROM audit_event WHERE action='drift.resolved'") == 1
        )


async def test_agent_bundle_drift_is_detected(pg_url: str) -> None:
    async with world(pg_url, vrrp_data()) as w:
        w.use_engines("se-a", "se-b", agents=True)
        await w.prime()
        await prime_bundles(w)
        healthy_vrrp(w)
        svc = service(w)
        await svc.poll_all()
        rows = await svc.list_status(principal(), TENANT)
        assert all(not r.drift and r.agent for r in rows), [r.drift for r in rows]
        w.agents["se-b"].files["keepalived/keepalived.conf"] += "# edited\n"
        w.agents["se-b"].files["frr/extra.conf"] = "x"
        await svc.poll_all()
        drift = {r.name: r.drift for r in await svc.list_status(principal(), TENANT)}
        assert drift["se-a"] == []
        kinds = {d["kind"] for d in drift["se-b"]}
        assert {"agent_file", "agent_unexpected_file"} <= kinds


async def test_the_connection_load_is_stored_with_the_agent_status(pg_url: str) -> None:
    """The autoscaler (ADR 0194) reads the load from the stored agent status."""
    async with world(pg_url, vrrp_data()) as w:
        w.use_engines("se-a", "se-b", agents=True)
        await w.prime()
        await prime_bundles(w)
        healthy_vrrp(w)
        w.engines["se-a"].curr_conns, w.engines["se-a"].max_conn = 300, 1000
        svc = service(w)
        await svc.poll_all()
        rows = {r.name: r for r in await svc.list_status(principal(), TENANT)}
        agent_a, agent_b = rows["se-a"].agent, rows["se-b"].agent
        assert agent_a is not None and agent_b is not None
        assert agent_a["load"] == {"conns": 300, "max_conn": 1000}
        assert "load" not in agent_b  # that engine did not report one


async def test_status_requires_tenant_access(pg_url: str) -> None:
    from switchyard.services.errors import PermissionDeniedError

    async with world(pg_url) as w:
        other = principal(tenant=TENANT.__class__(int=99))
        with pytest.raises(PermissionDeniedError):
            await service(w).list_status(other, TENANT)


async def test_scheduler_has_one_leader_and_does_not_pile_up(pg_url: str) -> None:
    async with world(pg_url) as w:
        schedule = {POLL_STATUS_JOB: timedelta(seconds=10)}
        a, b = Scheduler(w.db.admin, schedule), Scheduler(w.db.admin, schedule)
        try:
            assert await a.tick() == [POLL_STATUS_JOB]
            assert await b.tick() == [], "only the leader enqueues"
            assert await a.tick() == [], "a queued job is not scheduled twice"
            await a.resign()
            assert await b.try_lead(), "leadership moves when the leader resigns"
        finally:
            await a.resign()
            await b.resign()


async def test_poll_status_job_runs_through_the_worker(pg_url: str) -> None:
    async with world(pg_url) as w:
        handlers = build_handlers(w.db, w.pipeline, None, service(w))
        scheduler = Scheduler(w.db.admin, {POLL_STATUS_JOB: timedelta(seconds=10)})
        try:
            await scheduler.tick()
        finally:
            await scheduler.resign()
        worker = Worker(w.db.admin, handlers, worker_id="t")
        assert await worker.run_once()
        assert await scalar(w, "SELECT count(*) FROM engine_status") == 2
        assert await scalar(w, "SELECT result->>'polled' FROM job WHERE kind='poll_status'") == "2"


async def test_status_rows_are_isolated_by_rls(pg_url: str) -> None:
    from switchyard.db.tenancy import tenant_session

    async with world(pg_url) as w:
        await service(w).poll_all()
        async with tenant_session(w.db.app, [TENANT.__class__(int=5)]) as s:
            assert (await s.execute(text("SELECT count(*) FROM engine_status"))).scalar_one() == 0
        async with tenant_session(w.db.app, [TENANT]) as s:
            assert (await s.execute(text("SELECT count(*) FROM engine_status"))).scalar_one() == 2


async def test_status_endpoints(pg_url: str) -> None:
    import httpx

    from switchyard.api.app import build_services, create_app
    from switchyard.domain.workflow import Role
    from switchyard.pipeline.engines import StaticEngineDirectory
    from tests.pipeline_env import GROUP, engine_record

    async with world(pg_url) as w:
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        services = build_services(w.db, directory)
        await services.status.poll_all()
        root = principal(Role.SYSTEM_ADMIN, "root")
        plaintext, _ = await services.tokens.create(root, TENANT, "t", Role.VIEWER)
        headers = {"Authorization": f"Bearer {plaintext}"}
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            r = await client.get(f"/api/v1/tenants/{TENANT}/status/engines", headers=headers)
            assert r.status_code == 200, r.text
            assert [e["engine"] for e in r.json()] == ["se-a", "se-b"]
            h = await client.get(f"/api/v1/tenants/{TENANT}/status/se-groups", headers=headers)
            assert h.status_code == 200, h.text
            group = h.json()[0]
            assert [e["engine"] for e in group["engines"]] == ["se-a", "se-b"]
            assert 0 <= group["score"] <= 100
            s = await client.get(
                f"/api/v1/tenants/{TENANT}/status/stream?follow=false", headers=headers
            )
            assert s.status_code == 200
            assert s.headers["content-type"].startswith("text/event-stream")
            assert s.text.startswith("id: 1\nevent: status\ndata: ")
            data = json.loads(s.text.split("data: ", 1)[1].split("\n", 1)[0])
            assert len(data) == 2
            assert "event: vs_state\ndata: " in s.text
