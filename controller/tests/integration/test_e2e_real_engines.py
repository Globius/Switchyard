# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The whole stack against two real service engines (HAProxy + Data Plane API in Docker).

PostgreSQL holds the state, the real apply pipeline renders, validates with the engines' own
HAProxy, pushes through the Data Plane API, watches the canary with real health checks and
reverts a bad change. Only the clock is fast: the canary window is a few seconds.
"""

import copy
import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio

from switchyard.adapters.dataplane import DataPlaneEngine
from switchyard.adapters.secrets import MemorySecrets
from switchyard.domain.changes import Action, ChangeOp, ObjectType
from switchyard.domain.models import AdminState
from switchyard.domain.workflow import ChangeSetState, Role
from switchyard.pipeline.apply import ApplyPipeline
from switchyard.pipeline.engines import DbEngineDirectory
from switchyard.render.haproxy import render_haproxy
from switchyard.services.engines import EngineService
from switchyard.services.members import MemberService
from tests.factories import base_data
from tests.lab import API_PASSWORD, API_USER, Lab, Se
from tests.pipeline_env import GROUP, POOL, TENANT, VS, World, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")

CERT = "wild-example-test"


def data() -> dict:  # type: ignore[type-arg]
    d = copy.deepcopy(base_data())
    d["se_groups"][0]["canary_seconds"] = 8
    d["health_monitors"] = [
        {
            "id": "hm-fast",
            "tenant_id": "t-acme",
            "name": "fast-tcp",
            "type": "tcp",
            "interval_s": 1,
            "timeout_s": 1,
            "rise": 1,
            "fall": 2,
        }
    ]
    d["pools"][0]["health_monitor_id"] = "hm-fast"
    d["pools"][0]["members"] = [{"address": "192.0.2.51", "port": 8080}]
    return d


@pytest.fixture(scope="module")
def engines(lab_network: Lab) -> Iterator[tuple[Se, Se]]:
    with lab_network.scope():
        lab_network.start_backend("A", "192.0.2.51")
        yield (
            lab_network.start_se("se-1", "192.0.2.11", certs=(CERT,)),
            lab_network.start_se("se-2", "192.0.2.12", certs=(CERT,)),
        )


@pytest_asyncio.fixture
async def stack(pg_url: str, engines: tuple[Se, Se]) -> AsyncIterator[World]:
    async with world(pg_url, data()) as w:
        registry = EngineService(w.db)
        for se in engines:
            await registry.register(
                principal(),
                TENANT,
                se_group_id=GROUP,
                name=se.name,
                dataplane_url=se.api_url,
                credentials_ref="kv/engines/lab",
            )
        directory = DbEngineDirectory(
            w.db, MemorySecrets({"kv/engines/lab": f"{API_USER}:{API_PASSWORD}"})
        )
        w.pipeline = ApplyPipeline(w.db, directory, canary_poll_s=0.5, lock_timeout_s=20)
        w.directory = directory  # type: ignore[attr-defined]
        yield w


def route(host: str, version: int) -> ChangeOp:
    return ChangeOp(
        object_type=ObjectType.VIRTUAL_SERVICE,
        object_id=VS,
        action=Action.UPDATE,
        body={
            "routes": [
                {"match": {"host": "app.example.test"}, "pool_id": str(POOL)},
                {"match": {"host": host}, "pool_id": str(POOL)},
            ]
        },
        expected_version=version,
    )


async def fingerprints(engines: tuple[Se, Se]) -> list[str | None]:
    out = []
    for se in engines:
        client = DataPlaneEngine(se.api_url, name=se.name, username=API_USER, password=API_PASSWORD)
        out.append(await client.fingerprint())
        await client.close()
    return out


async def test_good_change_reaches_both_engines_and_a_bad_one_is_reverted(
    stack: World, engines: tuple[Se, Se]
) -> None:
    w = stack

    # 1. a good change, from a never-managed engine pair
    report = await w.run_change([route("one.example.test", 1)])
    assert report.ok, report.error
    assert report.revision_number == 1
    good = render_haproxy(await w.snapshot(), GROUP)
    assert await fingerprints(engines) == [good.fingerprint, good.fingerprint]
    assert "one.example.test" in engines[0].exec("cat /etc/switchyard/haproxy/maps/web-host.map")

    # 2. a change that points the pool at a dead address: the backend goes from up to down while
    #    the canary is being watched, so the canary is reverted and se-2 is never touched
    dead = ChangeOp(
        object_type=ObjectType.POOL,
        object_id=POOL,
        action=Action.UPDATE,
        body={
            "members": [{"id": str(uuid.uuid4()), "address": "192.0.2.99", "port": 8080}],
        },
        expected_version=1,
    )
    bad = await w.run_change([dead])
    assert not bad.ok
    assert "went from up to down" in (bad.error or "")
    assert await fingerprints(engines) == [good.fingerprint, good.fingerprint], (
        "both reverted/untouched"
    )
    assert "192.0.2.99" not in engines[0].exec("cat /etc/switchyard/haproxy/haproxy.cfg")
    snapshot = await w.snapshot()
    assert [str(m.address) for m in snapshot.pools[0].members] == ["192.0.2.51"]
    record = (
        await w.service.list_change_sets(principal(), TENANT, states={ChangeSetState.FAILED})
    )[0]
    assert record.error is not None

    # 3. the stored state still renders and applies cleanly afterwards
    again = await w.run_change([route("two.example.test", 2)])
    assert again.ok, again.error
    final = render_haproxy(await w.snapshot(), GROUP)
    assert await fingerprints(engines) == [final.fingerprint, final.fingerprint]


async def test_runtime_drain_on_real_engines(stack: World, engines: tuple[Se, Se]) -> None:
    w = stack
    assert (await w.run_change([route("one.example.test", 1)])).ok
    member = (await w.snapshot()).pools[0].members[0].id
    result = await MemberService(w.db, w.directory).set_state(  # type: ignore[attr-defined]
        principal(Role.OPERATOR), TENANT, POOL, member, AdminState.DRAIN
    )
    assert (result.applied, result.failed) == (["se-1", "se-2"], {})
    for se in engines:
        client = DataPlaneEngine(se.api_url, name=se.name, username=API_USER, password=API_PASSWORD)
        assert [s.status for s in await client.server_statuses()] == ["DRAIN"]
        await client.close()
