"""HTTP-01: the rendered rule, the runtime map entries and the publisher, on a real HAProxy."""

import copy
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest

from switchyard.adapters.dataplane import DataPlaneEngine
from switchyard.adapters.fake_haproxy import FakeHaproxy
from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
from switchyard.domain.workflow import EngineState
from switchyard.pipeline.engines import EngineRecord, StaticEngineDirectory
from switchyard.render.haproxy import ACME_MAP_FILE, render_haproxy
from switchyard.services.http01 import EngineHttp01Publisher, Http01Error
from tests.factories import base_data
from tests.lab import API_PASSWORD, API_USER, Lab, Se
from tests.pipeline_env import GROUP, TENANT, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")
PATH = "/.well-known/acme-challenge/token-1"
ANSWER = "token-1.thumbprint"
EDGE = "192.0.2.20"


def rendered(mutate: Any = None) -> Any:
    data = copy.deepcopy(base_data())
    if mutate:
        mutate(data)
    return render_haproxy(snapshot_from_mapping(data), UUID(symbolic_uuid("seg-1")))


@pytest.fixture(scope="module")
def edge(lab_network: Lab) -> Iterator[Se]:
    with lab_network.scope():
        yield lab_network.start_se("se-http", EDGE, certs=("wild-example-test",))


def record(name: str, state: EngineState = EngineState.ENABLED) -> EngineRecord:
    return EngineRecord(**{**engine_record(name).__dict__, "state": state})


async def test_the_edge_answers_a_published_challenge_and_only_that(
    pg_url: str, edge: Se, lab_network: Lab
) -> None:
    engine = DataPlaneEngine(edge.api_url, name=edge.name, username=API_USER, password=API_PASSWORD)
    try:
        await engine.push(rendered().files)
        async with world(pg_url) as w:
            directory = StaticEngineDirectory({GROUP: [(record(edge.name), engine)]}, {})
            publisher = EngineHttp01Publisher(w.db, directory, TENANT)
            try:
                await publisher.publish("www.example.test", PATH, ANSWER)
                out = lab_network.curl(
                    "192.0.2.98",
                    f"req known http://{EDGE}{PATH}\n"
                    f"req unknown http://{EDGE}/.well-known/acme-challenge/other\n"
                    f"req elsewhere http://{EDGE}/",
                )
                assert out["known"] == f"{ANSWER}|200|"
                assert out["unknown"].split("|")[1] == "301", "other paths behave as before"
                assert out["elsewhere"].split("|")[1] == "301"
                assert PATH in edge.exec(f"cat /etc/switchyard/{ACME_MAP_FILE}")
                await publisher.withdraw("www.example.test", PATH)
                gone = lab_network.curl("192.0.2.98", f"req known http://{EDGE}{PATH}")
                assert gone["known"].split("|")[1] == "301"
            finally:
                await publisher.close()
        assert PATH not in edge.exec(f"cat /etc/switchyard/{ACME_MAP_FILE}")
    finally:
        await engine.close()


async def test_a_changed_value_replaces_the_old_one_and_close_withdraws_what_is_left(
    pg_url: str, edge: Se, lab_network: Lab
) -> None:
    engine = DataPlaneEngine(edge.api_url, name=edge.name, username=API_USER, password=API_PASSWORD)
    try:
        await engine.push(rendered().files)
        await engine.remove_map_entry(ACME_MAP_FILE, PATH)  # a missing entry is not an error
        await engine.set_map_entry(ACME_MAP_FILE, PATH, "first")
        await engine.set_map_entry(ACME_MAP_FILE, PATH, "second")
        assert lab_network.curl("192.0.2.98", f"req a http://{EDGE}{PATH}")["a"] == "second|200|"
        async with world(pg_url) as w:
            directory = StaticEngineDirectory({GROUP: [(record(edge.name), engine)]}, {})
            publisher = EngineHttp01Publisher(w.db, directory, TENANT)
            await publisher.publish("www.example.test", PATH, "third")
            await publisher.close()
        after = lab_network.curl("192.0.2.98", f"req a http://{EDGE}{PATH}")["a"]
        assert after.split("|")[1] == "301", "close() withdrew the entry"
    finally:
        await engine.close()


async def test_an_engine_without_the_rule_is_reported_with_the_way_out(pg_url: str) -> None:
    async with world(pg_url) as w:
        stale = FakeHaproxy("se-old")
        await stale.push({"haproxy/haproxy.cfg": "global\n    log stdout\n"})
        directory = StaticEngineDirectory({GROUP: [(record("se-old"), stale)]}, {})
        publisher = EngineHttp01Publisher(w.db, directory, TENANT)
        with pytest.raises(Http01Error, match="apply any change set"):
            await publisher.publish("www.example.test", PATH, ANSWER)
        await publisher.close()


async def test_every_enabled_engine_answers_and_disabled_ones_are_left_alone(
    pg_url: str,
) -> None:
    async with world(pg_url) as w:
        files = rendered().files
        engines = {n: FakeHaproxy(n) for n in ("se-a", "se-b", "se-off")}
        for engine in engines.values():
            await engine.push(files)
        directory = StaticEngineDirectory(
            {
                GROUP: [
                    (record("se-a"), engines["se-a"]),
                    (record("se-b"), engines["se-b"]),
                    (record("se-off", EngineState.DISABLED), engines["se-off"]),
                ]
            },
            {},
        )
        publisher = EngineHttp01Publisher(w.db, directory, TENANT)
        await publisher.publish("www.example.test", PATH, ANSWER)
        assert engines["se-a"].map_entries[ACME_MAP_FILE] == {PATH: ANSWER}
        assert engines["se-b"].map_entries[ACME_MAP_FILE] == {PATH: ANSWER}
        assert engines["se-off"].map_entries == {}
        await publisher.close()
        assert engines["se-a"].map_entries[ACME_MAP_FILE] == {}
        assert all(e.closed for n, e in engines.items() if n != "se-off")


async def test_a_tenant_without_a_port_80_service_has_nothing_to_publish_on(pg_url: str) -> None:
    def only_tls(data: dict[str, Any]) -> None:
        for vs in data["virtual_services"]:
            vs["ports"] = [p for p in vs["ports"] if p.get("tls")]

    data = copy.deepcopy(base_data())
    only_tls(data)
    async with world(pg_url, data) as w:
        directory = StaticEngineDirectory({GROUP: [(record("se-a"), FakeHaproxy("se-a"))]}, {})
        publisher = EngineHttp01Publisher(w.db, directory, TENANT)
        with pytest.raises(Http01Error, match="port 80"):
            await publisher.publish("www.example.test", PATH, ANSWER)
