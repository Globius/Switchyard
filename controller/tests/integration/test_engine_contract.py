# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The ``HaproxyEngine`` contract, run against the fake and against a real Data Plane API."""

import copy
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio

from switchyard.adapters.dataplane import DataPlaneEngine
from switchyard.adapters.fake_haproxy import FakeHaproxy
from switchyard.adapters.haproxy import (
    CONFIG_PATH,
    ConfigRejected,
    HaproxyEngine,
    HaproxyError,
    ReloadFailed,
    Unreachable,
)
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.render.haproxy import RenderResult, render_haproxy
from tests.factories import base_data
from tests.lab import API_PASSWORD, API_USER, Lab, Se

GROUP = UUID(symbolic_uuid("seg-1"))
CERT = "wild-example-test"


def render(mutate: Any = None) -> RenderResult:
    data = copy.deepcopy(base_data())
    if mutate:
        mutate(data)
    return render_haproxy(snapshot_from_mapping(data), GROUP)


def more_routes(data: dict[str, Any]) -> None:
    data["pools"].append(
        {
            "id": "pool-b",
            "tenant_id": "t-acme",
            "name": "second",
            "members": [{"address": "10.20.0.50", "port": 80}],
        }
    )
    data["virtual_services"][0]["routes"].append(
        {"match": {"host": "other.example.test"}, "pool_id": "pool-b"}
    )


@pytest.fixture(scope="module")
def real_se(lab_network: Lab) -> Iterator[Se]:
    with lab_network.scope():
        yield lab_network.start_se("se-1", "192.0.2.11", certs=(CERT,))


@pytest.fixture(scope="module")
def strict_se(lab_network: Lab) -> Iterator[Se]:
    """An engine whose kernel refuses to bind addresses it does not hold."""
    with lab_network.scope():
        yield lab_network.start_se("se-strict", "192.0.2.12", certs=(CERT,), nonlocal_bind=False)


@pytest_asyncio.fixture(params=["fake", "dataplane"])
async def engine(request: pytest.FixtureRequest) -> AsyncIterator[HaproxyEngine]:
    if request.param == "fake":
        fake = FakeHaproxy()
        yield fake
        await fake.close()
        return
    se: Se = request.getfixturevalue("real_se")
    client = DataPlaneEngine(se.api_url, name=se.name, username=API_USER, password=API_PASSWORD)
    yield client
    await client.close()


async def test_push_then_fingerprint_matches_render(engine: HaproxyEngine) -> None:
    first = render()
    result = await engine.push(first.files)
    assert result.fingerprint == first.fingerprint
    assert await engine.fingerprint() == first.fingerprint


async def test_push_replaces_the_running_configuration(engine: HaproxyEngine) -> None:
    first, second = render(), render(more_routes)
    assert first.fingerprint != second.fingerprint
    await engine.push(first.files)
    await engine.push(second.files, previous=first.files)
    assert await engine.fingerprint() == second.fingerprint


async def test_rejected_configuration_leaves_the_previous_one_running(
    engine: HaproxyEngine,
) -> None:
    good = render()
    await engine.push(good.files)
    bad = dict(good.files)
    bad[CONFIG_PATH] = good.files[CONFIG_PATH].replace(
        "frontend vs_web", "frontend vs_web\n    bogus directive"
    )
    with pytest.raises(ConfigRejected):
        await engine.push(bad)
    assert await engine.fingerprint() == good.fingerprint


async def test_validate_checks_without_applying(engine: HaproxyEngine) -> None:
    first, second = render(), render(more_routes)
    await engine.push(first.files)
    await engine.validate(second.files)
    assert await engine.fingerprint() == first.fingerprint


async def test_validate_reports_bad_configuration(engine: HaproxyEngine) -> None:
    good = render()
    bad = dict(good.files)
    bad[CONFIG_PATH] = good.files[CONFIG_PATH] + "\nbackend broken\n    nonsense\n"
    with pytest.raises(ConfigRejected):
        await engine.validate(bad)


async def test_server_statuses_list_every_configured_server(engine: HaproxyEngine) -> None:
    await engine.push(render().files)
    statuses = await engine.server_statuses()
    assert [(s.backend, s.server) for s in statuses] == [("pool_web", "10-20-0-11-8080")]


async def test_set_server_state_changes_reported_status(engine: HaproxyEngine) -> None:
    await engine.push(render().files)
    await engine.set_server_state("pool_web", "10-20-0-11-8080", "drain")
    assert [s.status for s in await engine.server_statuses()] == ["DRAIN"]
    await engine.set_server_state("pool_web", "10-20-0-11-8080", "ready")
    assert [s.status for s in await engine.server_statuses()] != ["DRAIN"]


async def test_unknown_server_is_an_error(engine: HaproxyEngine) -> None:
    await engine.push(render().files)
    if isinstance(engine, FakeHaproxy):
        pytest.skip("the fake accepts any server name")
    with pytest.raises(HaproxyError, match="not found"):
        await engine.set_server_state("pool_web", "nope", "drain")


async def test_stale_files_are_removed_when_previous_is_given(engine: HaproxyEngine) -> None:
    first, second = render(), render(more_routes)

    # the first render has one host route, the second two: both use the same host map file, so
    # compare against a render without any map at all
    def no_routes(data: dict[str, Any]) -> None:
        data["virtual_services"][0]["routes"] = []
        data["virtual_services"][0]["default_pool_id"] = "pool-web"

    bare = render(no_routes)
    assert "haproxy/maps/web-host.map" in first.files
    assert "haproxy/maps/web-host.map" not in bare.files
    await engine.push(first.files)
    await engine.push(bare.files, previous=first.files)
    assert await engine.fingerprint() == bare.fingerprint
    del second


class TestRealEngineOnly:
    @pytest_asyncio.fixture
    async def real(self, real_se: Se) -> AsyncIterator[DataPlaneEngine]:
        client = DataPlaneEngine(
            real_se.api_url, name=real_se.name, username=API_USER, password=API_PASSWORD
        )
        yield client
        await client.close()

    async def test_files_land_where_the_config_expects_them(
        self, real: DataPlaneEngine, real_se: Se
    ) -> None:
        rendered = render()
        await real.push(rendered.files)
        maps = real_se.exec("ls /etc/switchyard/haproxy/maps")
        assert "web-host.map" in maps, "dots in names would be rewritten to underscores"
        assert (
            real_se.exec("cat /etc/switchyard/haproxy/maps/web-host.map")
            == rendered.files["haproxy/maps/web-host.map"]
        )
        assert "web.crtlist" in real_se.exec("ls /etc/switchyard/certs")

    async def test_removed_files_are_deleted_from_the_engine(
        self, real: DataPlaneEngine, real_se: Se
    ) -> None:
        with_map = render()

        def bare(data: dict[str, Any]) -> None:
            data["virtual_services"][0]["routes"] = []
            data["virtual_services"][0]["default_pool_id"] = "pool-web"

        without_map = render(bare)
        await real.push(with_map.files)
        await real.push(without_map.files, previous=with_map.files)
        assert "web-host.map" not in real_se.exec("ls /etc/switchyard/haproxy/maps")

    async def test_unreachable_engine(self) -> None:
        client = DataPlaneEngine("http://127.0.0.1:9", name="nowhere", timeout_s=1)
        with pytest.raises(Unreachable):
            await client.fingerprint()
        await client.close()

    async def test_wrong_credentials_are_reported(self, real_se: Se) -> None:
        client = DataPlaneEngine(real_se.api_url, name="bad", username="admin", password="wrong")
        with pytest.raises(HaproxyError, match="401"):
            await client.fingerprint()
        await client.close()

    async def test_reload_whose_new_worker_cannot_start_is_reported(self, strict_se: Se) -> None:
        engine = DataPlaneEngine(
            strict_se.api_url,
            name=strict_se.name,
            username=API_USER,
            password=API_PASSWORD,
            reload_timeout_s=20,
        )
        try:
            files = render().files
            # Staging creates the crt-list, and the Data Plane API reloads once for that (a
            # legitimate new worker). The pid to compare with is the one after it.
            await engine.validate(files)
            before = await engine.info()
            with pytest.raises(ReloadFailed, match="previous configuration"):
                await engine.push(files)  # binds 192.0.2.20, which this engine lacks
            assert (await engine.info()).pid == before.pid, "the old worker keeps serving"

            def own_address(data: dict[str, Any]) -> None:
                data["virtual_services"][0]["vip"] = strict_se.ip

            recovered = render(own_address)
            await engine.push(recovered.files)
            assert (await engine.info()).pid != before.pid
            assert await engine.fingerprint() == recovered.fingerprint
        finally:
            await engine.close()

    async def test_rewritten_config_still_carries_the_fingerprint(
        self, real: DataPlaneEngine, real_se: Se
    ) -> None:
        rendered = render()
        await real.push(rendered.files)
        on_disk = real_se.exec("cat /etc/switchyard/haproxy/haproxy.cfg")
        assert on_disk != rendered.files[CONFIG_PATH], "the API normalises the file"
        assert rendered.fingerprint in on_disk


async def test_fake_injected_failures() -> None:
    fake = FakeHaproxy()
    fake.fail_next("unreachable")
    with pytest.raises(Unreachable):
        await fake.push(render().files)
    fake.fail_next("rejected")
    with pytest.raises(ConfigRejected):
        await fake.validate(render().files)
    assert fake.files == {}


@pytest.fixture
def fresh_se(lab_network: Lab) -> Iterator[Se]:
    """An engine that has never been pushed to, so the crt-list does not exist yet."""
    with lab_network.scope():
        yield lab_network.start_se("se-fresh", "192.0.2.13", certs=(CERT,))


async def test_staging_and_pushing_files_leaves_no_stray_reload_behind(fresh_se: Se) -> None:
    """Creating a crt-list makes the Data Plane API reload after its delay; nothing may land later.

    A stray reload replaces the HAProxy worker a second time, which the canary reads as a crash
    (the end-to-end test failed about one run in four before the adapter waited for it).
    """
    import asyncio

    client = DataPlaneEngine(
        fresh_se.api_url, name=fresh_se.name, username=API_USER, password=API_PASSWORD
    )
    try:
        files = render().files
        await client.validate(files)  # stages the files: the crt-list is created here
        staged = (await client.info()).pid
        for _ in range(30):  # the Data Plane API's reload delay is one second
            await asyncio.sleep(0.1)
            assert (await client.info()).pid == staged, "a reload fired after validate returned"
        await client.push(files)
        pushed = (await client.info()).pid
        assert pushed != staged
        for _ in range(30):
            await asyncio.sleep(0.1)
            assert (await client.info()).pid == pushed, "a reload fired after push returned"
    finally:
        await client.close()
