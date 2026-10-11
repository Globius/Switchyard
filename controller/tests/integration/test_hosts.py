"""Proxmox host state and placement, over a real database and the fake Proxmox."""

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
