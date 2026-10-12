"""Automatic scale-out of an SE group (ADR 0194): decide, deploy, enroll, join."""

import copy
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from switchyard.adapters.agent import BundleSigner, FakeAgent
from switchyard.adapters.pki import LocalCa
from switchyard.adapters.proxmox import FakeProxmox
from switchyard.adapters.secrets import MemorySecrets
from switchyard.db.ops_models import (
    AutoscaleStateRow,
    EngineStatusRow,
    JobRow,
    ServiceEngineRow,
)
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import EngineState, JobState
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.autoscale import ENROLL_STALL, Autoscaler
from switchyard.services.changesets import REAPPLY_JOB
from switchyard.services.enrollment import EnrollmentService
from switchyard.services.infra import DEPLOY_SE_GROUP_JOB, DeployRequest, InfraService
from tests.integration.test_infra import infra_data
from tests.pipeline_env import GROUP, TENANT, World, world

pytestmark = pytest.mark.usefixtures("owner_engine")

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
POLICY: dict[str, Any] = {
    "max_engines": 3,
    "scale_out_at": 70,
    "sustain_s": 300,
    "cooldown_s": 900,
    "deploy": {
        "nodes": ["pve1"],
        "template_vmid": 9000,
        "mgmt_pool_id": "vp-mgmt",
        "credentials_ref": "kv/se/dpapi",
    },
    "enroll": {"controller_url": "https://ctrl.example.test:8443", "ca_ref": "kv/ca"},
}
ENROLL_DIR = "/etc/switchyard/agent"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class Rig:
    def __init__(self, w: World, directory: StaticEngineDirectory, clock: Clock) -> None:
        self.w, self.directory, self.clock = w, directory, clock
        self.pve = FakeProxmox()
        secrets = MemorySecrets({"kv/se/dpapi": "switchyard:s3cret", "kv/ca": "CA-PEM"})
        enrollment = EnrollmentService(
            w.db, LocalCa(), BundleSigner.generate().public_b64(), clock=clock
        )
        self.scaler = Autoscaler(w.db, directory, self.pve, enrollment, secrets, clock=clock)

    async def engines(self, load: int = 80, count: int = 2, healthy: bool = True) -> None:
        """Insert ``count`` enabled engines that use ``load`` percent of a 1000 connection limit."""
        async with admin_session(self.w.db.admin) as s:
            for i in range(1, count + 1):
                engine = ServiceEngineRow(
                    id=uuid.uuid4(),
                    tenant_id=TENANT,
                    se_group_id=GROUP,
                    name=f"edge-{i}",
                    dataplane_url=f"https://10.30.0.{9 + i}:5556",
                    mgmt_ip=f"10.30.0.{9 + i}",
                    agent_url=f"10.30.0.{9 + i}:7443",
                )
                s.add(engine)
                await s.flush()
                s.add(
                    EngineStatusRow(
                        engine_id=engine.id,
                        tenant_id=TENANT,
                        se_group_id=GROUP,
                        name=engine.name,
                        reachable=True,
                        members=[],
                        agent={
                            "haproxy_healthy": healthy,
                            "bgp": [],
                            "vrrp": [],
                            "load": {"conns": load * 10, "max_conn": 1000},
                        },
                        drift=[],
                    )
                )

    async def set_load(self, load: int) -> None:
        async with admin_session(self.w.db.admin) as s:
            for status in (await s.execute(select(EngineStatusRow))).scalars():
                status.agent = {
                    **(status.agent or {}),
                    "load": {"conns": load * 10, "max_conn": 1000},
                }

    async def set_max(self, engines: int) -> None:
        async with admin_session(self.w.db.admin) as s:
            await s.execute(
                text(
                    "UPDATE se_group SET autoscale = jsonb_set(autoscale, '{max_engines}', "
                    f"'{engines}')"
                )
            )

    async def state(self) -> AutoscaleStateRow:
        async with admin_session(self.w.db.admin) as s:
            row = await s.get(AutoscaleStateRow, GROUP)
            assert row is not None
            s.expunge(row)
            return row

    async def jobs(self, kind: str) -> list[JobRow]:
        async with admin_session(self.w.db.admin) as s:
            return list((await s.execute(select(JobRow).where(JobRow.kind == kind))).scalars())

    async def tick(self) -> dict[str, int]:
        return await self.scaler.run()

    async def scale_out(self) -> None:
        """Get from a busy group to a queued deploy job."""
        await self.tick()
        self.clock.advance(seconds=301)
        assert (await self.tick())["scaled_out"] == 1

    async def run_deploy(self) -> uuid.UUID:
        """Do what the worker does with the queued deploy job; returns the new engine's id."""
        (job,) = await self.jobs(DEPLOY_SE_GROUP_JOB)
        result = await InfraService(self.w.db, self.pve).deploy_se_group(
            DeployRequest.from_payload(job.payload)
        )
        async with admin_session(self.w.db.admin) as s:
            row = await s.get(JobRow, job.id)
            assert row is not None
            row.state, row.result = JobState.SUCCEEDED.value, result
            new = (
                await s.execute(
                    select(ServiceEngineRow).where(ServiceEngineRow.name == result["created"][-1])
                )
            ).scalar_one()
            return new.id


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    data = infra_data()
    data["se_groups"][0]["autoscale"] = copy.deepcopy(POLICY)
    async with world(pg_url, data) as w:
        yield Rig(w, StaticEngineDirectory({}), Clock())


async def test_a_group_must_stay_busy_for_the_sustain_time(rig: Rig) -> None:
    await rig.engines(load=80)
    assert (await rig.tick())["scaled_out"] == 0
    state = await rig.state()
    assert (state.decision, state.utilization) == ("busy", 80)
    assert await rig.jobs(DEPLOY_SE_GROUP_JOB) == []


async def test_a_quiet_group_resets_the_clock(rig: Rig) -> None:
    await rig.engines(load=80)
    await rig.tick()
    await rig.set_load(10)
    rig.clock.advance(seconds=301)
    await rig.tick()
    state = await rig.state()
    assert (state.decision, state.busy_since) == ("idle", None)
    assert await rig.jobs(DEPLOY_SE_GROUP_JOB) == []


async def test_a_busy_group_queues_one_staged_deploy(rig: Rig) -> None:
    await rig.engines(load=80)
    await rig.scale_out()
    (job,) = await rig.jobs(DEPLOY_SE_GROUP_JOB)
    request = DeployRequest.from_payload(job.payload)
    assert (request.count, request.staged, request.nodes) == (3, True, ("pve1",))
    assert request.credentials_ref == "kv/se/dpapi"
    assert (await rig.state()).decision == "scaling"
    rig.clock.advance(seconds=60)
    await rig.tick()  # the deploy is still queued: nothing more is queued
    assert len(await rig.jobs(DEPLOY_SE_GROUP_JOB)) == 1


async def test_the_whole_path_from_load_to_a_joined_engine(rig: Rig) -> None:
    await rig.engines(load=90)
    await rig.scale_out()
    new_id = await rig.run_deploy()
    async with admin_session(rig.w.db.admin) as s:
        new = await s.get(ServiceEngineRow, new_id)
        assert new is not None
        assert new.state == EngineState.DISABLED.value  # staged: not in rollouts or polls
        vmid = new.vmid
    assert vmid is not None

    rig.clock.advance(seconds=30)
    await rig.tick()  # enrollment is sent through the guest agent
    files = {p: c for (v, p), c in rig.pve.guest_files.items() if v == vmid}
    assert files[f"{ENROLL_DIR}/controller-ca.crt"] == "CA-PEM\n"
    env = files[f"{ENROLL_DIR}/enroll.env"]
    assert "CONTROLLER_URL=https://ctrl.example.test:8443" in env
    assert "DP_USER=switchyard" in env and "DP_PASSWORD=s3cret" in env
    assert files[f"{ENROLL_DIR}/enroll.token"].startswith("syenr_")
    argv = [c for v, c in rig.pve.guest_commands if v == vmid]
    assert ("chmod", "600", f"{ENROLL_DIR}/enroll.token") in argv
    assert argv[-1] == ("systemctl", "start", "sy-agent-enroll.service")
    assert (await rig.state()).enroll_sent_at is not None

    sent = len(rig.pve.guest_commands)
    rig.clock.advance(seconds=30)
    await rig.tick()  # not up yet: the token is not sent again
    assert len(rig.pve.guest_commands) == sent

    rig.directory._agents[new_id] = FakeAgent("edge-3")  # type: ignore[attr-defined]
    rig.clock.advance(seconds=30)  # the agent has enrolled and answers
    assert (await rig.tick())["joined"] == 1
    async with admin_session(rig.w.db.admin) as s:
        engine = await s.get(ServiceEngineRow, new_id)
        assert engine is not None and engine.state == EngineState.ENABLED.value
    (reapply,) = await rig.jobs(REAPPLY_JOB)
    assert reapply.payload["se_group_id"] == str(GROUP)
    state = await rig.state()
    assert (state.decision, state.pending_engine_id) == ("cooldown", None)
    assert state.last_scaled_at == rig.clock.now


async def test_the_cooldown_and_the_limit_stop_further_scale_out(rig: Rig) -> None:
    await rig.engines(load=90)
    await rig.scale_out()
    new_id = await rig.run_deploy()
    await rig.tick()
    rig.directory._agents[new_id] = FakeAgent("edge-3")  # type: ignore[attr-defined]
    await rig.tick()  # joined
    await rig.set_load(90)
    await rig.set_max(5)
    await rig.tick()  # busy again: the sustain clock starts over after a scale-out
    rig.clock.advance(seconds=301)
    await rig.tick()
    assert (await rig.state()).decision == "cooldown"  # 15 minutes have not passed
    assert len(await rig.jobs(DEPLOY_SE_GROUP_JOB)) == 1
    await rig.set_max(3)
    rig.clock.advance(seconds=900)
    await rig.tick()
    assert (await rig.state()).decision == "at_max"  # three engines is the limit now
    assert len(await rig.jobs(DEPLOY_SE_GROUP_JOB)) == 1


async def test_an_unhealthy_group_is_not_given_more_engines(rig: Rig) -> None:
    await rig.engines(load=90, healthy=False)
    await rig.tick()
    rig.clock.advance(seconds=301)
    assert (await rig.tick())["blocked"] == 1
    state = await rig.state()
    assert state.decision == "blocked" and "health" in (state.reason or "")
    assert await rig.jobs(DEPLOY_SE_GROUP_JOB) == []


async def test_a_failed_deploy_is_reported_and_starts_the_cooldown(rig: Rig) -> None:
    await rig.engines(load=90)
    await rig.scale_out()
    (job,) = await rig.jobs(DEPLOY_SE_GROUP_JOB)
    async with admin_session(rig.w.db.admin) as s:
        row = await s.get(JobRow, job.id)
        assert row is not None
        row.state, row.last_error = JobState.DEAD.value, "no capacity"
    assert (await rig.tick())["blocked"] == 1
    state = await rig.state()
    assert state.decision == "blocked" and "no capacity" in (state.reason or "")
    assert state.pending_job_id is None and state.last_scaled_at == rig.clock.now


async def test_an_engine_that_never_enrolls_is_reported_and_blocks_further_deploys(
    rig: Rig,
) -> None:
    await rig.engines(load=90)
    await rig.scale_out()
    await rig.run_deploy()
    await rig.tick()
    rig.clock.advance(seconds=ENROLL_STALL.total_seconds() + 1)
    assert (await rig.tick())["blocked"] == 1
    state = await rig.state()
    assert state.decision == "blocked" and "20 minutes" in (state.reason or "")
    assert state.pending_engine_id is not None  # still pending: no second engine is added
    assert len(await rig.jobs(DEPLOY_SE_GROUP_JOB)) == 1


async def test_a_guest_agent_that_is_not_up_yet_is_retried(rig: Rig) -> None:
    await rig.engines(load=90)
    await rig.scale_out()
    await rig.run_deploy()
    rig.pve.fail_on.add("guest_write_file")
    await rig.tick()
    assert (await rig.state()).enroll_sent_at is None
    rig.clock.advance(seconds=30)
    await rig.tick()
    assert (await rig.state()).enroll_sent_at is not None


async def test_a_disabled_policy_does_nothing(rig: Rig) -> None:
    await rig.engines(load=95)
    async with admin_session(rig.w.db.admin) as s:
        await s.execute(
            text("UPDATE se_group SET autoscale = jsonb_set(autoscale, '{enabled}', 'false')")
        )
    await rig.tick()
    rig.clock.advance(seconds=1000)
    await rig.tick()
    assert (await rig.state()).decision == "off"
    assert await rig.jobs(DEPLOY_SE_GROUP_JOB) == []
