# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

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


# -- TLS-ALPN-01 -------------------------------------------------------------------------------


async def test_the_edge_presents_the_challenge_certificate_only_to_acme_tls(
    pg_url: str, edge: Se
) -> None:
    from switchyard.adapters.acme import challenge_certificate
    from switchyard.services.tlsalpn import EngineTlsAlpnPublisher

    engine = DataPlaneEngine(edge.api_url, name=edge.name, username=API_USER, password=API_PASSWORD)

    def handshake(alpn: str) -> str:
        return edge.exec(
            f"echo | openssl s_client -connect {EDGE}:443 -servername www.example.test "
            f"-alpn {alpn} 2>/dev/null | openssl x509 -noout -subject -ext subjectAltName"
        )

    try:
        await engine.push(rendered().files)
        assert "CN=www.example.test" not in handshake("acme-tls/1")
        async with world(pg_url) as w:
            directory = StaticEngineDirectory({GROUP: [(record(edge.name), engine)]}, {})
            publisher = EngineTlsAlpnPublisher(w.db, directory, TENANT)
            try:
                await publisher.publish(
                    "www.example.test", challenge_certificate("www.example.test", "tok.thumb")
                )
                presented = handshake("acme-tls/1")
                assert "CN=www.example.test" in presented, presented
                assert "DNS:www.example.test" in presented
            finally:
                await publisher.close()
        assert "CN=www.example.test" not in handshake("acme-tls/1"), "the certificate is gone again"
    finally:
        await engine.close()


async def test_alpn_publisher_needs_the_rendered_alpn_token_and_a_443_service(
    pg_url: str,
) -> None:
    from switchyard.services.tlsalpn import EngineTlsAlpnPublisher, TlsAlpnError

    async with world(pg_url) as w:
        old = FakeHaproxy("se-old")
        await old.push({"haproxy/haproxy.cfg": "global\n    log stdout\n"})
        directory = StaticEngineDirectory({GROUP: [(record("se-old"), old)]}, {})
        with pytest.raises(TlsAlpnError, match="apply any change set"):
            await EngineTlsAlpnPublisher(w.db, directory, TENANT).publish("x.example.test", "pem")

        current = FakeHaproxy("se-new")
        await current.push(rendered().files)
        engines = StaticEngineDirectory(
            {GROUP: [(record("se-new"), current), (record("se-off", EngineState.DISABLED), old)]},
            {},
        )
        publisher = EngineTlsAlpnPublisher(w.db, engines, TENANT)
        await publisher.publish("x.example.test", "pem")
        [(sni, pem)] = current.challenge_certs["certs/web.crtlist"].values()
        assert (sni, pem) == ("x.example.test", "pem")
        await publisher.close()
        assert current.challenge_certs["certs/web.crtlist"] == {}


async def test_alpn_publisher_has_nothing_to_present_on_without_a_443_service(
    pg_url: str,
) -> None:
    from switchyard.services.tlsalpn import EngineTlsAlpnPublisher, TlsAlpnError

    data = copy.deepcopy(base_data())
    for vs in data["virtual_services"]:
        vs["ports"] = [
            {k: v for k, v in p.items() if k != "https_redirect_port"}
            for p in vs["ports"]
            if not p.get("tls")
        ]
    async with world(pg_url, data) as w:
        directory = StaticEngineDirectory({GROUP: [(record("se-a"), FakeHaproxy("se-a"))]}, {})
        with pytest.raises(TlsAlpnError, match="port 443"):
            await EngineTlsAlpnPublisher(w.db, directory, TENANT).publish("x.example.test", "pem")


def three_tls_services(data: dict[str, Any], *, http3: bool = False) -> None:
    """Two more HTTPS services (the second with HTTP/3) next to ``web``: three crt-lists."""
    for i, (name, vip) in enumerate((("api", "192.0.2.21"), ("shop", "192.0.2.22"))):
        extra = copy.deepcopy(data["virtual_services"][0])
        extra.update({"id": f"vs-{name}", "name": name, "vip": vip})
        extra["routes"] = [{"match": {"host": f"{name}.example.test"}, "pool_id": "pool-web"}]
        if i == 1 and http3:
            extra["ports"][0]["http3"] = True
        data["virtual_services"].append(extra)


async def test_one_challenge_certificate_per_engine_for_every_crt_list(pg_url: str) -> None:
    """Three TLS services mean three crt-lists, but the certificate is loaded once (not 3 times)."""
    from switchyard.services.tlsalpn import EngineTlsAlpnPublisher

    data = copy.deepcopy(base_data())
    three_tls_services(data)
    async with world(pg_url, data) as w:
        engine = FakeHaproxy("se-a")
        await engine.push(
            render_haproxy(snapshot_from_mapping(data), UUID(symbolic_uuid("seg-1"))).files
        )
        directory = StaticEngineDirectory({GROUP: [(record("se-a"), engine)]}, {})
        publisher = EngineTlsAlpnPublisher(w.db, directory, TENANT)
        await publisher.publish("x.example.test", "pem")
        lists = sorted(engine.challenge_certs)
        assert lists == ["certs/api.crtlist", "certs/shop.crtlist", "certs/web.crtlist"]
        assert all(len(engine.challenge_certs[c]) == 1 for c in lists)
        assert list(engine.cert_loads.values()) == [1], "loaded once on the engine"
        await publisher.publish("x.example.test", "pem")  # repeating is fine
        assert len(engine.cert_loads) == 1 and all(
            len(engine.challenge_certs[c]) == 1 for c in lists
        )
        await publisher.close()
        assert all(not v for v in engine.challenge_certs.values())


async def test_a_missing_crt_list_leaves_nothing_loaded(pg_url: str) -> None:
    from switchyard.services.tlsalpn import EngineTlsAlpnPublisher, TlsAlpnError

    data = copy.deepcopy(base_data())
    three_tls_services(data)
    async with world(pg_url, data) as w:
        engine = FakeHaproxy("se-a")
        files = dict(
            render_haproxy(snapshot_from_mapping(data), UUID(symbolic_uuid("seg-1"))).files
        )
        del files["certs/shop.crtlist"]
        await engine.push(files)
        directory = StaticEngineDirectory({GROUP: [(record("se-a"), engine)]}, {})
        publisher = EngineTlsAlpnPublisher(w.db, directory, TENANT)
        with pytest.raises(TlsAlpnError, match=r"shop\.crtlist"):
            await publisher.publish("x.example.test", "pem")
        assert all(not v for v in engine.challenge_certs.values()) and not engine.cert_loads
        await publisher.close()


async def test_a_real_edge_with_three_tls_services_takes_the_challenge_certificate(
    pg_url: str, edge: Se
) -> None:
    from switchyard.adapters.acme import challenge_certificate
    from switchyard.services.tlsalpn import EngineTlsAlpnPublisher

    engine = DataPlaneEngine(edge.api_url, name=edge.name, username=API_USER, password=API_PASSWORD)
    data = copy.deepcopy(base_data())
    three_tls_services(data)
    h3 = copy.deepcopy(base_data())
    three_tls_services(h3, http3=True)
    try:
        await engine.push(
            render_haproxy(snapshot_from_mapping(h3), UUID(symbolic_uuid("seg-1"))).files
        )
        async with world(pg_url, data) as w:
            directory = StaticEngineDirectory({GROUP: [(record(edge.name), engine)]}, {})
            publisher = EngineTlsAlpnPublisher(w.db, directory, TENANT)
            pem = challenge_certificate("www.example.test", "tok.thumb")
            try:
                await publisher.publish("www.example.test", pem)
                await publisher.publish("www.example.test", pem)  # a retry after a failed run
            finally:
                await publisher.close()
    finally:
        await engine.close()
