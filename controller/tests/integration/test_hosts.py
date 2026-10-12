# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Proxmox host state and placement, over a real database and the fake Proxmox."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from switchyard.adapters.proxmox import FakeProxmox, NodeInfo, StorageInfo
from switchyard.api.app import build_services, create_app
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from switchyard.services.engines import EngineService
from switchyard.services.errors import NotFoundError, PermissionDeniedError
from switchyard.services.hosts import HostService
from tests.pipeline_env import GROUP, TENANT, World, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
GIB = 1024**3


def cluster() -> FakeProxmox:
    pve = FakeProxmox()
    pve.nodes = [
        NodeInfo("pve2", False),
        NodeInfo("pve1", True, 0.25, 16, 8 * GIB, 64 * GIB, 3600),
    ]
    pve.storages["pve1"] = [
        StorageInfo("pve1", "local-lvm", "lvmthin", True, ("images", "rootdir"), 100, 40, 60)
    ]
    return pve


async def place(w: World, node: str) -> None:
    """Register ``se-a`` on ``node`` (with a VM id) and ``se-b`` by hand, without a node."""
    engines = EngineService(w.db)
    for name in ("se-a", "se-b"):
        await engines.register(
            ROOT,
            TENANT,
            se_group_id=GROUP,
            name=name,
            dataplane_url=f"https://192.0.2.{10 if name == 'se-a' else 11}:5555",
        )
    async with admin_session(w.db.admin) as s:
        await s.execute(
            text("UPDATE service_engine SET pve_node = :n, vmid = 9100 WHERE name = 'se-a'"),
            {"n": node},
        )


async def test_placement_lists_nodes_with_state_storage_and_the_groups_engines(
    pg_url: str,
) -> None:
    async with world(pg_url) as w:
        await place(w, "pve1")
        hosts = HostService(w.db, EngineService(w.db), cluster())
        got = await hosts.placement(ROOT, TENANT, GROUP)
        assert got.available and got.reason is None
        assert [(n.name, n.online) for n in got.nodes] == [("pve1", True), ("pve2", False)]
        pve1, pve2 = got.nodes
        assert (pve1.cpu, pve1.max_cpu, pve1.mem_total) == (0.25, 16, 64 * GIB)
        assert [(s.name, s.available) for s in pve1.storage] == [("local-lvm", 60)]
        assert [(e.name, e.vmid) for e in pve1.engines] == [("se-a", 9100)]
        assert pve2.engines == () and pve2.storage == ()


async def test_an_engine_on_an_unlisted_node_is_shown_offline(pg_url: str) -> None:
    async with world(pg_url) as w:
        await place(w, "pve9")
        hosts = HostService(w.db, EngineService(w.db), cluster())
        got = await hosts.placement(ROOT, TENANT, GROUP)
        assert [(n.name, n.online) for n in got.nodes] == [
            ("pve1", True),
            ("pve2", False),
            ("pve9", False),
        ]
        assert [e.name for e in got.nodes[-1].engines] == ["se-a"]


async def test_without_proxmox_the_layout_is_known_but_not_the_state(pg_url: str) -> None:
    async with world(pg_url) as w:
        await place(w, "pve1")
        for pve in (None, FakeProxmox(fail_on={"list_nodes"})):
            hosts = HostService(w.db, EngineService(w.db), pve)
            got = await hosts.placement(ROOT, TENANT, GROUP)
            assert not got.available and got.reason
            assert [(n.name, n.online, len(n.engines)) for n in got.nodes] == [("pve1", None, 1)]
            assert await hosts.online_by_node() == {}


async def test_answers_are_cached_for_the_ttl(pg_url: str) -> None:
    async with world(pg_url) as w:
        now = [0.0]
        pve = cluster()
        hosts = HostService(w.db, EngineService(w.db), pve, ttl_s=10, clock=lambda: now[0])
        await hosts.online_by_node()
        await hosts.online_by_node()
        assert [c for c in pve.calls if c[0] == "list_nodes"] == [("list_nodes", "")]
        now[0] = 11.0
        pve.nodes = [NodeInfo("pve1", False)]
        assert await hosts.online_by_node() == {"pve1": False}


async def test_unknown_group_and_foreign_tenant_are_refused(pg_url: str) -> None:
    async with world(pg_url) as w:
        hosts = HostService(w.db, EngineService(w.db), cluster())
        with pytest.raises(NotFoundError):
            await hosts.placement(ROOT, TENANT, uuid.uuid4())
        outsider = Principal(
            actor="user:eve", role=Role.TENANT_ADMIN, tenant_ids=frozenset({uuid.uuid4()})
        )
        with pytest.raises(PermissionDeniedError):
            await hosts.placement(outsider, TENANT, GROUP)


@pytest_asyncio.fixture
async def client(pg_url: str) -> AsyncIterator[tuple[httpx.AsyncClient, dict[str, str], World]]:
    async with world(pg_url) as w:
        await place(w, "pve1")
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        services = build_services(w.db, directory, proxmox=cluster())
        plaintext, _ = await services.tokens.create(ROOT, TENANT, "t", Role.VIEWER)
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as c:
            yield c, {"Authorization": f"Bearer {plaintext}"}, w


async def test_api_reports_host_online_state_and_placement(
    client: tuple[httpx.AsyncClient, dict[str, str], World],
) -> None:
    c, h, _ = client
    engines = (await c.get("/api/v1/tenants/acme/engines", headers=h)).json()
    assert {e["name"]: e["host_online"] for e in engines}["se-a"] is True
    assert {e["name"]: e["host_online"] for e in engines}["se-b"] is None  # no node recorded
    r = await c.get(f"/api/v1/tenants/acme/se-groups/{GROUP}/placement", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["nodes"][0]["storage"][0]["content"] == ["images", "rootdir"]
    assert body["nodes"][0]["engines"][0]["name"] == "se-a"
    missing = await c.get(f"/api/v1/tenants/acme/se-groups/{uuid.uuid4()}/placement", headers=h)
    assert missing.status_code == 404


async def test_engine_addresses_follow_the_policy(
    client: tuple[httpx.AsyncClient, dict[str, str], World], monkeypatch: pytest.MonkeyPatch
) -> None:
    c, _, w = client
    from switchyard.services.engines import EngineService

    monkeypatch.setenv("SWITCHYARD_ENGINE_POLICY", "strict")
    monkeypatch.delenv("SWITCHYARD_ENGINE_CIDRS", raising=False)
    svc = EngineService(w.db)

    async def register(url: str, **kw: str) -> None:
        await svc.register(ROOT, TENANT, se_group_id=GROUP, name="p1", dataplane_url=url, **kw)

    # the test group has no mgmt network and no allow-list: nothing is allowed
    with pytest.raises(ValueError, match="management network"):
        await register("https://192.0.2.9:5555")
    monkeypatch.setenv("SWITCHYARD_ENGINE_CIDRS", "192.0.2.0/24")
    with pytest.raises(ValueError, match="https"):
        await register("http://192.0.2.9:5555")
    with pytest.raises(ValueError, match="IP address"):
        await register("https://se-1.example.test:5555")
    with pytest.raises(ValueError, match="management network"):
        await register("https://169.254.169.254:5555")
    with pytest.raises(ValueError, match="agent_url"):
        await register("https://192.0.2.9:5555", agent_url="10.9.9.9:7443")
    with pytest.raises(ValueError, match="mgmt_ip"):
        await register("https://192.0.2.9:5555", mgmt_ip="10.9.9.9")
    await register("https://192.0.2.9:5555", agent_url="192.0.2.9:7443", mgmt_ip="192.0.2.9")
    assert c is not None


async def test_tenants_get_a_fixed_engine_error_and_administrators_the_real_one(
    client: tuple[httpx.AsyncClient, dict[str, str], World],
) -> None:
    from sqlalchemy import text as sql

    c, viewer, w = client
    root_token, _ = await build_services(w.db, StaticEngineDirectory({})).tokens.create(
        ROOT, None, "root", Role.SYSTEM_ADMIN
    )
    async with admin_session(w.db.admin) as s:
        await s.execute(
            sql(
                "INSERT INTO engine_status (engine_id, tenant_id, se_group_id, name, reachable, "
                "error, members, drift, observed_at) SELECT id, tenant_id, se_group_id, name, "
                "false, 'connect to 10.9.9.9:5555 failed: refused', '[]', '[]', now() "
                "FROM service_engine LIMIT 1"
            )
        )
    url = "/api/v1/tenants/acme/status/engines"
    seen = (await c.get(url, headers=viewer)).json()
    assert seen and "10.9.9.9" not in seen[0]["error"] and seen[0]["error"]
    full = (await c.get(url, headers={"Authorization": f"Bearer {root_token}"})).json()
    assert "10.9.9.9" in full[0]["error"]


async def test_containers_are_off_until_a_system_administrator_enables_them(
    client: tuple[httpx.AsyncClient, dict[str, str], World], monkeypatch: pytest.MonkeyPatch
) -> None:
    from switchyard.services.settings import SystemSettings

    monkeypatch.delenv("SWITCHYARD_ALLOW_LXC", raising=False)
    c, viewer, w = client
    services = build_services(w.db, StaticEngineDirectory({}))
    root_token, _ = await services.tokens.create(ROOT, None, "root", Role.SYSTEM_ADMIN)
    root = {"Authorization": f"Bearer {root_token}"}
    admin_token, _ = await services.tokens.create(ROOT, TENANT, "admin", Role.TENANT_ADMIN)
    tenant_admin = {"Authorization": f"Bearer {admin_token}"}
    body = {
        "count": 1,
        "nodes": ["pve1"],
        "template_vmid": 9000,
        "mgmt_pool_id": str(uuid.uuid4()),
        "guest_kind": "lxc",
    }
    deploy = f"/api/v1/tenants/acme/se-groups/{GROUP}/deploy"

    assert (await c.get("/api/v1/me", headers=viewer)).json()["features"] == {"lxc_guests": False}
    refused = await c.post(deploy, json=body, headers=tenant_admin)
    assert refused.status_code == 403 and refused.json()["code"] == "lxc_disabled"
    assert "disabled" in refused.json()["detail"]
    # only system administrators read or change the setting
    assert (await c.get("/api/v1/system/settings", headers=viewer)).status_code == 403
    denied = await c.patch(
        "/api/v1/system/settings", json={"allow_lxc_guests": True}, headers=viewer
    )
    assert denied.status_code == 403
    assert (await c.get("/api/v1/system/settings", headers=root)).json() == {
        "allow_lxc_guests": False,
        "allow_lxc_guests_forced": False,
    }

    on = await c.patch("/api/v1/system/settings", json={"allow_lxc_guests": True}, headers=root)
    assert on.status_code == 200 and on.json()["allow_lxc_guests"] is True
    assert (await c.get("/api/v1/me", headers=viewer)).json()["features"] == {"lxc_guests": True}
    accepted = await c.post(deploy, json=body, headers=tenant_admin)
    assert accepted.status_code == 202, accepted.text
    async with admin_session(w.db.admin) as s:
        audited = await s.execute(
            text("SELECT count(*) FROM audit_event WHERE action = 'system.settings_changed'")
        )
        assert audited.scalar_one() == 1

    await c.patch("/api/v1/system/settings", json={"allow_lxc_guests": False}, headers=root)
    assert (await c.post(deploy, json=body, headers=tenant_admin)).status_code == 403
    # the VM kind is never affected
    assert (
        await c.post(deploy, json=body | {"guest_kind": "qemu"}, headers=tenant_admin)
    ).status_code == 202

    # a deployment can force it on, as with break-glass
    monkeypatch.setenv("SWITCHYARD_ALLOW_LXC", "1")
    forced = (await c.get("/api/v1/system/settings", headers=root)).json()
    assert forced == {"allow_lxc_guests": True, "allow_lxc_guests_forced": True}
    assert (await c.post(deploy, json=body, headers=tenant_admin)).status_code == 202
    monkeypatch.delenv("SWITCHYARD_ALLOW_LXC")
    assert await SystemSettings(w.db).lxc_allowed() is False


async def test_the_command_line_can_set_a_setting(pg_url: str) -> None:
    from typer.testing import CliRunner

    from switchyard.cli import app
    from switchyard.services.settings import SystemSettings

    async with world(pg_url) as w:
        result = await asyncio.to_thread(
            CliRunner().invoke,
            app,
            ["setting", "allow_lxc_guests", "true", "--database-url", pg_url],
        )
        assert result.exit_code == 0, result.output
        assert await SystemSettings(w.db).lxc_allowed() is True
        bad = await asyncio.to_thread(
            CliRunner().invoke, app, ["setting", "nope", "true", "--database-url", pg_url]
        )
        assert bad.exit_code == 2
