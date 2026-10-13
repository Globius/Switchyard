# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Engine rolls: start, advance with fake actions, pause on failure, resume and abort (ADR 0205)."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.db.ops_models import ServiceEngineRow
from switchyard.db.tenancy import admin_session
from switchyard.domain import roll as machine
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from switchyard.services.roll import RollActionError, RollContext, RollService
from tests.pipeline_env import GROUP, TENANT, World, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

V1 = "/api/v1"
ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)


class FakeActions:
    """Every phase succeeds at once unless told otherwise."""

    def __init__(self) -> None:
        self.executed: list[machine.Command] = []
        self.fail_deploy = False
        self.refuse_policy = False
        self.smoke_ok = True

    async def execute(
        self, ctx: RollContext, command: machine.Command, jobs: dict[str, Any]
    ) -> None:
        if self.refuse_policy and isinstance(command, machine.Deploy):
            raise RollActionError("the SE group has no autoscale settings")
        self.executed.append(command)

    async def observe(
        self, ctx: RollContext, step: machine.EngineStep, jobs: dict[str, Any], now: float
    ) -> machine.Facts:
        return machine.Facts(
            now,
            deployed=f"{step.old}-new" if step.phase is machine.Phase.DEPLOYING else None,
            deploy_failed=self.fail_deploy and step.phase is machine.Phase.DEPLOYING,
            enrolled=True,
            reapplied=True,
            group_score=95,
            smoke_ok=self.smoke_ok,
            retired=True,
        )


class Rig:
    def __init__(
        self,
        w: World,
        services: ApiServices,
        client: httpx.AsyncClient,
        actions: FakeActions,
        roller: RollService,
    ) -> None:
        self.w, self.services, self.client = w, services, client
        self.actions, self.roller = actions, roller

    async def token(self, role: Role) -> dict[str, str]:
        plaintext, _ = await self.services.tokens.create(
            ROOT, TENANT, f"t{uuid.uuid4().hex[:8]}", role
        )
        return {"Authorization": f"Bearer {plaintext}"}

    async def run_ticks(self, n: int = 12) -> None:
        for _ in range(n):
            await self.roller.tick()


async def seed_engines(w: World, count: int = 2) -> None:
    async with admin_session(w.db.admin) as s:
        for i in range(1, count + 1):
            s.add(
                ServiceEngineRow(
                    id=uuid.uuid4(),
                    tenant_id=TENANT,
                    se_group_id=GROUP,
                    name=f"edge-{i}",
                    dataplane_url=f"https://10.30.0.{9 + i}:5556",
                    mgmt_ip=f"10.30.0.{9 + i}",
                    agent_url=f"10.30.0.{9 + i}:7443",
                )
            )


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        services = build_services(w.db, directory)
        await seed_engines(w)
        actions = FakeActions()
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            yield Rig(w, services, client, actions, RollService(w.db, actions))


def url(path: str) -> str:
    return f"{V1}/tenants/{TENANT}{path}"


async def start(rig: Rig, h: dict[str, str]) -> dict[str, Any]:
    res = await rig.client.post(
        url(f"/se-groups/{GROUP}/rolls"), json={"template_vmid": 9770}, headers=h
    )
    assert res.status_code == 201, res.text
    body: dict[str, Any] = res.json()
    return body


async def test_a_roll_replaces_every_engine_in_turn_and_finishes(rig: Rig) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    made = await start(rig, admin)
    engines = [s["old"] for s in made["steps"]]
    assert len(engines) >= 2 and made["status"] == "running"
    await rig.run_ticks(len(engines) * 10)
    roll = (await rig.client.get(url("/rolls"), headers=admin)).json()[0]
    assert roll["status"] == "done" and all(s["phase"] == "done" for s in roll["steps"])
    deploys = [c for c in rig.actions.executed if isinstance(c, machine.Deploy)]
    retires = [c for c in rig.actions.executed if isinstance(c, machine.Retire)]
    assert [d.replaces for d in deploys] == engines and [r.engine for r in retires] == engines
    # one engine at a time: the second deploy comes after the first retire
    kinds = [type(c).__name__ for c in rig.actions.executed]
    assert kinds.index("Retire") < kinds.index("Deploy", kinds.index("Deploy") + 1)


async def test_a_failed_step_pauses_and_resume_or_abort_follow(rig: Rig) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    made = await start(rig, admin)
    rig.actions.fail_deploy = True
    await rig.run_ticks(4)
    roll = (await rig.client.get(url("/rolls"), headers=admin)).json()[0]
    assert roll["status"] == "paused" and "could not be deployed" in roll["reason"]
    assert roll["steps"][0]["phase"] == "deploying"  # the old engine is untouched
    assert not any(isinstance(c, machine.Retire) for c in rig.actions.executed)

    rig.actions.fail_deploy = False
    resumed = await rig.client.post(
        url(f"/rolls/{made['id']}"), json={"action": "resume"}, headers=admin
    )
    assert resumed.status_code == 200 and resumed.json()["status"] == "running"
    await rig.run_ticks(60)
    assert (await rig.client.get(url("/rolls"), headers=admin)).json()[0]["status"] == "done"
    again = await rig.client.post(
        url(f"/rolls/{made['id']}"), json={"action": "abort"}, headers=admin
    )
    assert again.status_code == 409


async def test_an_action_that_cannot_run_pauses_with_its_reason(rig: Rig) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    await start(rig, admin)
    rig.actions.refuse_policy = True
    await rig.run_ticks(2)
    roll = (await rig.client.get(url("/rolls"), headers=admin)).json()[0]
    assert roll["status"] == "paused" and "no autoscale settings" in roll["reason"]


async def test_a_failed_smoke_check_keeps_the_old_engine(rig: Rig) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    await start(rig, admin)
    rig.actions.smoke_ok = False
    await rig.run_ticks(10)
    roll = (await rig.client.get(url("/rolls"), headers=admin)).json()[0]
    assert roll["status"] == "paused" and "smoke" in roll["reason"]
    assert not any(isinstance(c, machine.Retire) for c in rig.actions.executed)


async def test_only_one_roll_per_group_and_abort_ends_it(rig: Rig) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    made = await start(rig, admin)
    second = await rig.client.post(
        url(f"/se-groups/{GROUP}/rolls"), json={"template_vmid": 9770}, headers=admin
    )
    assert second.status_code == 409
    aborted = await rig.client.post(
        url(f"/rolls/{made['id']}"), json={"action": "abort"}, headers=admin
    )
    assert aborted.json()["status"] == "aborted"
    await rig.run_ticks(3)
    assert rig.actions.executed == []  # nothing runs for an aborted roll
    assert (await start(rig, admin))["status"] == "running"  # a new one may start


async def test_operators_cannot_start_a_roll(rig: Rig) -> None:
    op = await rig.token(Role.OPERATOR)
    res = await rig.client.post(
        url(f"/se-groups/{GROUP}/rolls"), json={"template_vmid": 9770}, headers=op
    )
    assert res.status_code == 403
