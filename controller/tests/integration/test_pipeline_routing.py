"""The apply pipeline with routing: bundles for the agent, agent-based canary, reverts."""

import copy
from typing import Any

import pytest
from sqlalchemy import text

from switchyard.adapters.agent import BgpSession, FakeAgent, VrrpInstance
from switchyard.adapters.secrets import MemorySecrets
from switchyard.db.tenancy import admin_session
from switchyard.domain.changes import Action, ChangeOp, ObjectType
from switchyard.pipeline.apply import bundle_id
from tests.factories import base_data
from tests.pipeline_env import TENANT, World, add_host_route, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")

PROFILE_ID = "rp-1"


def vrrp_data() -> dict[str, Any]:
    d = copy.deepcopy(base_data())
    d["se_groups"][0].update(canary_seconds=0, ha_mode="vrrp", routing_profile_id=PROFILE_ID)
    d["routing_profiles"] = [
        {
            "id": PROFILE_ID,
            "tenant_id": "t-acme",
            "name": "routing",
            "kind": "vrrp",
            "vrid": 51,
            "interface": "eth1",
        }
    ]
    return d


def bgp_data(**peer: Any) -> dict[str, Any]:
    d = vrrp_data()
    d["se_groups"][0]["ha_mode"] = "bgp"
    d["routing_profiles"] = [
        {
            "id": PROFILE_ID,
            "tenant_id": "t-acme",
            "name": "routing",
            "kind": "bgp",
            "asn": 65010,
            "peers": [{"peer_ip": "10.30.0.1", "remote_asn": 65001, **peer}],
        }
    ]
    return d


def healthy_vrrp(w: World) -> None:
    for name, agent in w.agents.items():
        agent.vrrp = (VrrpInstance("sy_edge", "MASTER" if name == "se-a" else "BACKUP"),)


def healthy_bgp(w: World) -> None:
    for agent in w.agents.values():
        agent.bgp = (BgpSession("10.30.0.1", "Established", 65001),)


async def scalar(w: World, sql: str) -> Any:
    async with admin_session(w.db.admin) as s:
        return (await s.execute(text(sql))).scalar_one()


async def prime_bundles(w: World) -> None:
    """Put each agent on the current state, as a deployed group would be."""
    from switchyard.pipeline.bundles import engine_files
    from switchyard.render.routing import EngineSlot
    from tests.pipeline_env import CERT_SECRETS, GROUP

    snap = await w.snapshot()
    secrets = MemorySecrets({**CERT_SECRETS, "kv/bgp/pw": "example-password-1"})
    group = next(g for g in snap.se_groups if g.id == GROUP)
    for i, (name, agent) in enumerate(w.agents.items()):
        files = await engine_files(snap, group, EngineSlot(name, i, f"10.30.0.{11 + i}"), secrets)
        await agent.apply_bundle(bundle_id(files), files)


async def test_vrrp_bundles_are_pushed_per_engine_with_falling_priority(pg_url: str) -> None:
    async with world(pg_url, vrrp_data()) as w:
        w.use_engines("se-a", "se-b", agents=True)
        await w.prime()
        await prime_bundles(w)
        healthy_vrrp(w)
        report = await w.run_change([add_host_route("new.example.test")])
        assert report.ok, report.error
        a = w.agents["se-a"].files["keepalived/keepalived.conf"]
        b = w.agents["se-b"].files["keepalived/keepalived.conf"]
        assert "priority 150" in a and "priority 140" in b
        assert "192.0.2.20/24 dev eth1" in a
        assert report.groups[0].details["bundles"].keys() == {"se-a", "se-b"}


async def test_agent_that_rejects_the_bundle_fails_the_apply_and_reverts_haproxy(
    pg_url: str,
) -> None:
    async with world(pg_url, vrrp_data()) as w:
        w.use_engines("se-a", "se-b", agents=True)
        await w.prime()
        await prime_bundles(w)
        healthy_vrrp(w)
        original = w.engines["se-a"].files["haproxy/haproxy.cfg"]
        w.agents["se-a"].fail_next = "signature verification failed"
        report = await w.run_change([add_host_route("new.example.test")])
        assert not report.ok
        assert "agent rejected the routing bundle" in (report.error or "")
        assert w.engines["se-a"].files["haproxy/haproxy.cfg"] == original
        assert w.engines["se-b"].pushes == []
        assert await scalar(w, "SELECT count(*) FROM revision") == 0


async def test_canary_fails_when_vrrp_goes_to_fault_and_everything_is_reverted(pg_url: str) -> None:
    async with world(pg_url, vrrp_data()) as w:
        w.use_engines("se-a", "se-b", agents=True)
        await w.prime()
        await prime_bundles(w)
        healthy_vrrp(w)
        before = dict(w.agents["se-a"].files)
        fake: FakeAgent = w.agents["se-a"]
        real_apply = fake.apply_bundle

        async def apply_then_fault(bid: str, files: Any) -> Any:
            result = await real_apply(bid, files)
            fake.vrrp = (VrrpInstance("sy_edge", "FAULT"),)
            return result

        fake.apply_bundle = apply_then_fault  # type: ignore[method-assign,assignment]
        report = await w.run_change([add_host_route("new.example.test")])
        assert not report.ok
        assert "VRRP instance sy_edge is FAULT" in (report.error or "")
        assert fake.files == before, "the previous routing bundle was restored"
        assert w.agents["se-b"].files != {} and w.engines["se-b"].pushes == []


async def test_bgp_canary_waits_for_established_sessions(pg_url: str) -> None:
    async with world(pg_url, bgp_data(md5_ref="kv/bgp/pw")) as w:
        w.use_engines("se-a", "se-b", agents=True, secrets={"kv/bgp/pw": "example-password-1"})
        await w.prime()
        await prime_bundles(w)
        healthy_bgp(w)
        report = await w.run_change([add_host_route("new.example.test")])
        assert report.ok, report.error
        conf = w.agents["se-a"].files["frr/frr.conf"]
        assert "neighbor 10.30.0.1 password example-password-1" in conf
        assert "bgp router-id 10.30.0.11" in conf
        assert "bgp router-id 10.30.0.12" in w.agents["se-b"].files["frr/frr.conf"]


async def test_bgp_session_that_does_not_establish_fails_the_canary(pg_url: str) -> None:
    async with world(pg_url, bgp_data()) as w:
        w.use_engines("se-a", "se-b", agents=True)
        await w.prime()
        await prime_bundles(w)
        healthy_bgp(w)
        for agent in w.agents.values():
            agent.bgp = (BgpSession("10.30.0.1", "Active", 65001),)
        report = await w.run_change([add_host_route("new.example.test")])
        assert not report.ok
        assert "BGP peer 10.30.0.1 is Active" in (report.error or "")


async def test_withdrawn_routes_or_unhealthy_haproxy_fail_the_canary(pg_url: str) -> None:
    async with world(pg_url, vrrp_data()) as w:
        w.use_engines("se-a", "se-b", agents=True)
        await w.prime()
        await prime_bundles(w)
        healthy_vrrp(w)
        w.agents["se-a"].healthy = False
        report = await w.run_change([add_host_route("new.example.test")])
        assert not report.ok
        assert "health check is failing" in (report.error or "")


async def test_plan_errors_for_bgp_engines_without_router_id_or_secret(pg_url: str) -> None:
    async with world(pg_url, bgp_data(md5_ref="kv/bgp/pw")) as w:
        w.use_engines("se-a", "se-b", agents=True)  # no secret available
        await w.prime()
        report = await w.run_change([add_host_route("new.example.test")])
        assert not report.ok
        assert "secret kv/bgp/pw" in (report.error or "")


async def test_a_routing_profile_change_alone_reaches_the_agents(pg_url: str) -> None:
    async with world(pg_url, vrrp_data()) as w:
        w.use_engines("se-a", "se-b", agents=True)
        await w.prime()
        await prime_bundles(w)
        healthy_vrrp(w)
        op = ChangeOp(
            object_type=ObjectType.ROUTING_PROFILE,
            object_id=(await w.snapshot()).routing_profiles[0].id,
            action=Action.UPDATE,
            body={"vrid": 77},
            expected_version=1,
        )
        report = await w.run_change([op], principal())
        assert report.ok, report.error
        assert "virtual_router_id 77" in w.agents["se-a"].files["keepalived/keepalived.conf"]
        assert TENANT


async def test_engines_without_an_agent_are_left_to_their_own_routing(pg_url: str) -> None:
    async with world(pg_url, vrrp_data()) as w:
        w.use_engines("se-a", "se-b")  # no agents
        await w.prime()
        report = await w.run_change([add_host_route("new.example.test")])
        assert report.ok, report.error
        assert "bundles" not in report.groups[0].details


CERT_PATH = "certs/wild-example-test.pem"


async def test_certificates_reach_the_agents_before_haproxy_uses_them(pg_url: str) -> None:
    from tests.pipeline_env import CERT_SECRETS, FAKE_PEM

    async with world(pg_url, vrrp_data()) as w:
        new_ref = "kv/tenants/acme/certs/wild-example-test/v2"
        rotated = FAKE_PEM.replace("placeholder", "rotated")
        w.use_engines("se-a", "se-b", agents=True, secrets={**CERT_SECRETS, new_ref: rotated})
        await w.prime()
        await prime_bundles(w)
        healthy_vrrp(w)
        assert w.agents["se-a"].files[CERT_PATH] == CERT_SECRETS[next(iter(CERT_SECRETS))]
        seen: list[bool] = []

        async def on_push(engine: Any) -> None:
            seen.append(rotated in w.agents["se-a"].files[CERT_PATH])

        w.engines["se-a"].on_push = on_push
        cert_id = (await w.snapshot()).certificates[0].id
        op = ChangeOp(
            object_type=ObjectType.CERTIFICATE,
            object_id=cert_id,
            action=Action.UPDATE,
            body={"secret_ref": new_ref},
            expected_version=1,
        )
        report = await w.run_change([op], principal())
        assert report.ok, report.error
        assert (
            w.agents["se-a"].files[CERT_PATH] == rotated
            and w.agents["se-b"].files[CERT_PATH] == rotated
        )
        assert seen == [True], "the new certificate was on the engine before HAProxy was pushed"


async def test_missing_certificate_secret_fails_the_plan_before_anything_is_pushed(
    pg_url: str,
) -> None:
    async with world(pg_url, vrrp_data()) as w:
        w.use_engines("se-a", "se-b", agents=True)
        await w.prime()
        await prime_bundles(w)
        healthy_vrrp(w)
        cert_id = (await w.snapshot()).certificates[0].id
        op = ChangeOp(
            object_type=ObjectType.CERTIFICATE,
            object_id=cert_id,
            action=Action.UPDATE,
            body={"secret_ref": "kv/tenants/acme/certs/missing"},
            expected_version=1,
        )
        report = await w.run_change([op], principal())
        assert not report.ok
        assert "certificate wild-example-test not found" in (report.error or "")
        assert w.engines["se-a"].pushes == []
