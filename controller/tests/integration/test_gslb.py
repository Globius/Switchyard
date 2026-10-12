"""GSLB zones and wide IPs over a real database, with a fake PowerDNS (ADR 0160 to 0163)."""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from switchyard.adapters.powerdns import FakePowerDns
from switchyard.api.app import build_services, create_app
from switchyard.db.tenancy import admin_session
from switchyard.domain.gslb import Rrset
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from switchyard.services.gslb import GslbService, GslbSync
from tests.pipeline_env import TENANT, World, world

pytestmark = pytest.mark.usefixtures("owner_engine")

ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
V1 = "/api/v1/tenants/acme"
ZONE = "gslb.example.test"
MEMBERS = [{"address": "192.0.2.10", "site": "pve1"}, {"address": "192.0.2.20", "priority": 2}]


class Env:
    def __init__(self, w: World, client: httpx.AsyncClient, dns: FakePowerDns, svc: GslbService):
        self.w, self.client, self.dns, self.svc = w, client, dns, svc
        self.tokens: dict[str, dict[str, str]] = {}

    def h(self, who: str = "admin") -> dict[str, str]:
        return self.tokens[who]

    async def zone(self, name: str = ZONE) -> httpx.Response:
        return await self.client.post(
            f"{V1}/gslb-zones",
            json={"name": name, "nameservers": ["ns1.example.test"]},
            headers=self.h("root"),
        )

    async def wide(self, name: str = "shop", **extra: Any) -> httpx.Response:
        body = {"name": name, "algorithm": "failover", "members": MEMBERS} | extra
        return await self.client.post(
            f"{V1}/gslb-zones/{ZONE}/wide-ips", json=body, headers=self.h()
        )


@pytest_asyncio.fixture
async def env(pg_url: str) -> AsyncIterator[Env]:
    async with world(pg_url) as w:
        dns = FakePowerDns()
        svc = GslbService(w.db, dns)
        services = build_services(w.db, StaticEngineDirectory({}), gslb=svc)
        other = (await services.tenants.create(ROOT, "globex")).id
        e = Env(w, httpx.AsyncClient(), dns, svc)
        for who, tenant, role in (
            ("root", None, Role.SYSTEM_ADMIN),
            ("admin", TENANT, Role.TENANT_ADMIN),
            ("viewer", TENANT, Role.VIEWER),
            ("globex", other, Role.TENANT_ADMIN),
        ):
            token = (await services.tokens.create(ROOT, tenant, who, role))[0]
            e.tokens[who] = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(services)), base_url="http://api"
        ) as client:
            e.client = client
            yield e


async def test_only_system_administrators_create_zones_and_they_cannot_overlap(env: Env) -> None:
    denied = await env.client.post(
        f"{V1}/gslb-zones",
        json={"name": ZONE, "nameservers": ["ns1.example.test"]},
        headers=env.h(),
    )
    assert denied.status_code == 403 and denied.json()["required_role"] == "system-admin"
    made = await env.zone()
    assert made.status_code == 201 and made.json()["name"] == ZONE
    assert env.dns.zones["gslb.example.test."] == {}
    assert env.dns.nameservers["gslb.example.test."] == ["ns1.example.test"]
    assert (await env.zone()).status_code == 409
    assert (await env.zone("sub.gslb.example.test")).status_code == 409  # a child of a taken zone
    assert (await env.zone("example.test")).status_code == 409  # a parent of a taken zone
    assert (await env.zone("other.example.org")).status_code == 201
    bad = await env.client.post(
        f"{V1}/gslb-zones", json={"name": "Bad Zone", "nameservers": []}, headers=env.h("root")
    )
    assert bad.status_code == 422 and bad.json()["errors"][0]["path"] == "name"
    # tenants see only their own zones
    assert [
        z["name"]
        for z in (await env.client.get(f"{V1}/gslb-zones", headers=env.h("viewer"))).json()
    ] == [
        ZONE,
        "other.example.org",
    ]
    assert (
        await env.client.get("/api/v1/tenants/globex/gslb-zones", headers=env.h("globex"))
    ).json() == []


async def test_a_wide_ip_is_published_changed_switched_off_and_deleted(env: Env) -> None:
    await env.zone()
    made = await env.wide()
    assert made.status_code == 201, made.text
    body = made.json()
    assert (
        body["fqdn"] == "shop.gslb.example.test"
        and body["last_error"] is None
        and body["applied_at"]
    )
    (rrset,) = await env.dns.get_rrsets(ZONE)
    assert rrset.name == "shop.gslb.example.test." and rrset.ttl == 30
    assert rrset.records == [
        "A \"ifportup(443, {{'192.0.2.10'}, {'192.0.2.20'}}, {timeout=1000})\""
    ]
    assert (await env.wide()).status_code == 409
    viewer = await env.client.post(
        f"{V1}/gslb-zones/{ZONE}/wide-ips",
        json={"name": "x", "members": MEMBERS},
        headers=env.h("viewer"),
    )
    assert viewer.status_code == 403

    patched = await env.client.patch(
        f"{V1}/gslb-zones/{ZONE}/wide-ips/shop",
        json={"algorithm": "all", "ttl": 60},
        headers=env.h(),
    )
    assert (
        patched.json()["algorithm"] == "all"
        and patched.json()["members"][0]["address"] == "192.0.2.10"
    )
    (rrset,) = await env.dns.get_rrsets(ZONE)
    assert rrset.ttl == 60 and "selector='all'" in rrset.records[0]

    off = await env.client.patch(
        f"{V1}/gslb-zones/{ZONE}/wide-ips/shop", json={"enabled": False}, headers=env.h()
    )
    assert off.json()["enabled"] is False and await env.dns.get_rrsets(ZONE) == []
    status = (await env.client.get(f"{V1}/gslb/status", headers=env.h("viewer"))).json()
    assert status[0]["state"] == "off"
    await env.client.patch(
        f"{V1}/gslb-zones/{ZONE}/wide-ips/shop", json={"enabled": True}, headers=env.h()
    )
    assert len(await env.dns.get_rrsets(ZONE)) == 1
    assert (
        await env.client.delete(f"{V1}/gslb-zones/{ZONE}/wide-ips/shop", headers=env.h())
    ).status_code == 204
    assert await env.dns.get_rrsets(ZONE) == []
    assert (
        await env.client.delete(f"{V1}/gslb-zones/{ZONE}/wide-ips/shop", headers=env.h())
    ).status_code == 404


async def test_invalid_wide_ips_are_refused_with_field_paths_and_never_reach_powerdns(
    env: Env,
) -> None:
    await env.zone()
    for members, path in (
        ([{"address": "192.0.2.10'}) x"}], "members[0].address"),
        ([{"virtual_service": "nope"}], "members[0].virtual_service"),
        ([{"address": "192.0.2.10", "subnets": ["x"]}], "members[0].subnets[0]"),
    ):
        r = await env.wide(members=members, algorithm="subnet")
        assert r.status_code == 422 and path in {e["path"] for e in r.json()["errors"]}, path
    assert (await env.wide(name="Bad Name")).status_code == 422
    assert (await env.wide(health={"kind": "http", "path": "/x'y"})).status_code == 422
    assert await env.dns.get_rrsets(ZONE) == []
    assert (
        await env.client.post(
            f"{V1}/gslb-zones/missing.example.test/wide-ips",
            json={"name": "a", "members": MEMBERS},
            headers=env.h(),
        )
    ).status_code == 404


async def test_a_member_can_follow_a_virtual_services_vip(env: Env) -> None:
    await env.zone()
    async with admin_session(env.w.db.admin) as s:
        vs = (await s.execute(text("SELECT name, vip FROM virtual_service LIMIT 1"))).one()
    members = [{"virtual_service": vs[0], "site": "pve1"}]
    made = await env.wide(algorithm="all", members=members)
    assert made.status_code == 201, made.text
    vip = str(vs[1]).split("/")[0]
    (rrset,) = await env.dns.get_rrsets(ZONE)
    assert f"'{vip}'" in rrset.records[0]
    # the VIP moves: the sync job follows it
    async with admin_session(env.w.db.admin) as s:
        await s.execute(
            text("UPDATE virtual_service SET vip = '203.0.113.99' WHERE name = :n"), {"n": vs[0]}
        )
    status = (await env.client.get(f"{V1}/gslb/status", headers=env.h())).json()
    assert status[0]["state"] == "drift"  # reported, not yet fixed
    assert (await GslbSync(env.svc, env.w.db).run())["written"] == 1
    (rrset,) = await env.dns.get_rrsets(ZONE)
    assert "203.0.113.99" in rrset.records[0]
    assert (await env.client.get(f"{V1}/gslb/status", headers=env.h())).json()[0][
        "state"
    ] == "in_sync"
    assert (await GslbSync(env.svc, env.w.db).run())[
        "written"
    ] == 0  # nothing changed: nothing written


async def test_drift_is_reported_and_only_an_explicit_sync_fixes_it(env: Env) -> None:
    await env.zone()
    await env.wide()
    key = ("shop.gslb.example.test.", "LUA")
    env.dns.zones["gslb.example.test."][key] = Rrset(
        key[0], "LUA", 30, ["A \"ifportup(443, {'6.6.6.6'})\""]
    )
    status = (await env.client.get(f"{V1}/gslb/status", headers=env.h("viewer"))).json()
    assert [(s["fqdn"], s["state"]) for s in status] == [("shop.gslb.example.test", "drift")]
    assert (await GslbSync(env.svc, env.w.db).run())["written"] == 0  # the job does not touch drift
    assert "6.6.6.6" in (await env.dns.get_rrsets(ZONE))[0].records[0]
    denied = await env.client.post(f"{V1}/gslb/sync", headers=env.h("viewer"))
    assert denied.status_code == 403
    fixed = await env.client.post(f"{V1}/gslb/sync", headers=env.h())
    assert fixed.json() == {"written": 1, "failed": 0}
    assert "192.0.2.10" in (await env.dns.get_rrsets(ZONE))[0].records[0]


async def test_a_powerdns_outage_is_recorded_and_repaired_by_the_job(env: Env) -> None:
    await env.zone()
    env.dns.fail = True
    made = await env.wide()
    assert made.status_code == 201 and "not reachable" in made.json()["last_error"]
    status = (await env.client.get(f"{V1}/gslb/status", headers=env.h())).json()
    assert status[0]["state"] in {"error", "unknown"}
    env.dns.fail = False
    assert (await GslbSync(env.svc, env.w.db).run()) == {"tenants": 1, "written": 1, "failed": 0}
    assert (await env.client.get(f"{V1}/gslb/status", headers=env.h())).json()[0][
        "state"
    ] == "in_sync"


async def test_dnssec_returns_ds_records_and_is_tenant_admin_only(env: Env) -> None:
    await env.zone()
    assert (
        await env.client.post(f"{V1}/gslb-zones/{ZONE}/dnssec", headers=env.h("viewer"))
    ).status_code == 403
    before = (await env.client.get(f"{V1}/gslb-zones/{ZONE}/ds", headers=env.h("viewer"))).json()
    assert before == {"zone": ZONE, "dnssec": False, "ds": []}
    signed = (await env.client.post(f"{V1}/gslb-zones/{ZONE}/dnssec", headers=env.h())).json()
    assert signed["dnssec"] is True and signed["ds"][0].startswith("gslb.example.test. IN DS")
    zones = (await env.client.get(f"{V1}/gslb-zones", headers=env.h("viewer"))).json()
    assert zones[0]["dnssec"] is True


async def test_deleting_a_zone_removes_it_and_its_wide_ips(env: Env) -> None:
    await env.zone()
    await env.wide()
    denied = await env.client.delete(f"{V1}/gslb-zones/{ZONE}", headers=env.h())
    assert denied.status_code == 403
    assert (
        await env.client.delete(f"{V1}/gslb-zones/{ZONE}", headers=env.h("root"))
    ).status_code == 204
    assert "gslb.example.test." not in env.dns.zones
    assert (await env.client.get(f"{V1}/wide-ips", headers=env.h())).json() == []


async def test_gslb_not_wired_answers_404_after_the_role_check(pg_url: str) -> None:
    async with world(pg_url) as w:
        services = build_services(w.db, StaticEngineDirectory({}))
        token = (await services.tokens.create(ROOT, TENANT, "v", Role.VIEWER))[0]
        admin = (await services.tokens.create(ROOT, TENANT, "a", Role.TENANT_ADMIN))[0]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(services)), base_url="http://api"
        ) as client:
            read = await client.get(f"{V1}/wide-ips", headers={"Authorization": f"Bearer {token}"})
            assert read.status_code == 404 and read.json()["code"] == "not_enabled"
            write = await client.post(
                f"{V1}/gslb/sync", headers={"Authorization": f"Bearer {token}"}
            )
            assert write.status_code == 403
            ok_role = await client.post(
                f"{V1}/gslb/sync", headers={"Authorization": f"Bearer {admin}"}
            )
            assert ok_role.status_code == 404
