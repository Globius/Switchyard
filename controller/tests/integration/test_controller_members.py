"""Controller cluster members, with the external ESXi one: API, Patroni status, adopt (ADR 0051)."""

import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import yaml
from sqlalchemy import select

from switchyard.adapters.patroni import FakePatroni
from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.db.controller_models import ControllerMemberRow
from switchyard.db.ops_models import AuditEventRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.adopt import adopt, load_controller_members
from switchyard.services.context import Principal
from switchyard.services.controller_members import ControllerMembers
from tests.pipeline_env import GROUP, TENANT, World, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

V1 = "/api/v1"
ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
ESXI = {
    "name": "ctl-esxi",
    "address": "10.0.0.13",
    "platform": "esxi",
    "host": "esxi-01",
    "nofailover": True,
    "patroni_url": "http://10.0.0.13:8008",
}
PROXMOX = {
    "name": "ctl-1",
    "address": "10.0.0.11",
    "platform": "proxmox",
    "host": "pve1",
    "patroni_url": "http://10.0.0.11:8008",
}
CLUSTER = {
    "members": [
        {"name": "ctl-1", "role": "leader", "state": "running", "timeline": 4},
        {
            "name": "ctl-esxi",
            "role": "replica",
            "state": "streaming",
            "timeline": 4,
            "lag": 512,
            "tags": {"nofailover": True},
        },
    ]
}


class Rig:
    def __init__(
        self, w: World, services: ApiServices, client: httpx.AsyncClient, patroni: FakePatroni
    ) -> None:
        self.w, self.services, self.client, self.patroni = w, services, client, patroni

    async def token(self, role: Role) -> dict[str, str]:
        tenant = None if role is Role.SYSTEM_ADMIN else TENANT
        plaintext, _ = await self.services.tokens.create(
            ROOT, tenant, f"t{uuid.uuid4().hex[:8]}", role
        )
        return {"Authorization": f"Bearer {plaintext}"}

    async def add(self, h: dict[str, str], body: Mapping[str, object]) -> httpx.Response:
        return await self.client.post(f"{V1}/system/controller/members", json=body, headers=h)


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        services = build_services(w.db, directory)
        patroni = FakePatroni()
        services.controller_members = ControllerMembers(w.db, patroni)
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            yield Rig(w, services, client, patroni)


async def test_the_external_esxi_member_is_listed_beside_the_proxmox_ones(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    assert (await rig.add(root, PROXMOX)).status_code == 201
    made = await rig.add(root, ESXI)
    assert made.status_code == 201, made.text
    assert made.json()["external"] is True and made.json()["nofailover"] is True
    rig.patroni.clusters["http://10.0.0.11:8008"] = CLUSTER

    view = (await rig.client.get(f"{V1}/system/controller/members", headers=root)).json()
    assert view["cluster_reachable"] is True and view["leader"] == "ctl-1"
    by_name = {m["name"]: m for m in view["members"]}
    assert by_name["ctl-1"]["external"] is False
    assert by_name["ctl-1"]["status"]["database_role"] == "leader"
    esxi = by_name["ctl-esxi"]
    assert esxi["status"]["database_role"] == "replica" and esxi["status"]["lag_bytes"] == 512
    assert esxi["status"]["nofailover"] is True and esxi["in_cluster"] is True
    assert [m["name"] for m in view["members"]] == ["ctl-1", "ctl-esxi"]  # platform members first


async def test_a_member_patroni_does_not_list_is_marked(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.add(root, PROXMOX)
    await rig.add(root, ESXI)
    rig.patroni.clusters["http://10.0.0.11:8008"] = {"members": CLUSTER["members"][:1]}
    view = (await rig.client.get(f"{V1}/system/controller/members", headers=root)).json()
    esxi = next(m for m in view["members"] if m["name"] == "ctl-esxi")
    assert esxi["in_cluster"] is False and esxi["status"]["database_role"] is None


async def test_the_cluster_view_comes_from_the_first_member_that_answers(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.add(root, PROXMOX)
    await rig.add(root, ESXI)
    rig.patroni.down.add("http://10.0.0.11:8008")
    rig.patroni.clusters["http://10.0.0.13:8008"] = CLUSTER
    view = (await rig.client.get(f"{V1}/system/controller/members", headers=root)).json()
    assert view["cluster_reachable"] is True and view["leader"] == "ctl-1"


async def test_when_no_patroni_answers_the_members_still_list(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.add(root, PROXMOX)
    down = (await rig.client.get(f"{V1}/system/controller/members", headers=root)).json()
    assert down["cluster_reachable"] is False and "connection refused" in down["cluster_error"]
    assert down["members"][0]["in_cluster"] is None
    assert down["members"][0]["status"]["database_role"] is None

    other = await rig.token(Role.SYSTEM_ADMIN)
    nothing = {**PROXMOX, "name": "ctl-2", "patroni_url": None}
    gone = await rig.client.delete(
        f"{V1}/system/controller/members/{down['members'][0]['id']}",
        headers={**other, "If-Match": '"1"'},
    )
    assert gone.status_code == 204, gone.text
    added = await rig.add(other, nothing)
    assert added.status_code == 201, added.text
    quiet = (await rig.client.get(f"{V1}/system/controller/members", headers=root)).json()
    assert quiet["cluster_reachable"] is None and quiet["cluster_error"] is None, quiet


async def test_bad_members_are_refused_with_the_field(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    bad = await rig.add(root, {**ESXI, "address": "169.254.169.254", "patroni_url": "ftp://x"})
    assert bad.status_code == 422
    assert {e["path"] for e in bad.json()["errors"]} == {"address", "patroni_url"}
    assert (await rig.add(root, {**ESXI, "platform": "vmware"})).status_code == 422
    assert (await rig.add(root, ESXI)).status_code == 201
    assert (await rig.add(root, ESXI)).status_code == 409


async def test_change_and_remove_need_the_current_version(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    member = (await rig.add(root, ESXI)).json()
    url = f"{V1}/system/controller/members/{member['id']}"
    stale = await rig.client.patch(
        url, json={"host": "esxi-02"}, headers={**root, "If-Match": '"9"'}
    )
    assert stale.status_code == 409
    done = await rig.client.patch(
        url, json={"host": "esxi-02", "platform": "other"}, headers={**root, "If-Match": '"1"'}
    )
    assert done.status_code == 200, done.text
    assert done.json()["host"] == "esxi-02" and done.json()["version"] == 2
    assert done.headers["etag"] == '"2"'
    invalid = await rig.client.patch(
        url, json={"address": "127.0.0.1"}, headers={**root, "If-Match": '"2"'}
    )
    assert invalid.status_code == 422
    gone = await rig.client.delete(url, headers={**root, "If-Match": '"2"'})
    assert gone.status_code == 204
    assert (await rig.client.delete(url, headers={**root, "If-Match": '"2"'})).status_code == 404
    assert (await rig.client.get(f"{V1}/system/controller/members", headers=root)).json()[
        "members"
    ] == []


async def test_only_system_administrators_see_or_change_members(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    member = (await rig.add(root, ESXI)).json()
    for role in (Role.VIEWER, Role.OPERATOR, Role.TENANT_ADMIN):
        h = await rig.token(role)
        assert (
            await rig.client.get(f"{V1}/system/controller/members", headers=h)
        ).status_code == 403
        assert (await rig.add(h, PROXMOX)).status_code == 403
        url = f"{V1}/system/controller/members/{member['id']}"
        assert (await rig.client.delete(url, headers={**h, "If-Match": '"1"'})).status_code == 403
    assert (await rig.client.get(f"{V1}/system/controller/members")).status_code == 401


async def test_changes_are_audited(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    member = (await rig.add(root, ESXI)).json()
    url = f"{V1}/system/controller/members/{member['id']}"
    await rig.client.patch(url, json={"host": "esxi-02"}, headers={**root, "If-Match": '"1"'})
    await rig.client.delete(url, headers={**root, "If-Match": '"2"'})
    async with admin_session(rig.w.db.admin) as session:
        rows = (
            await session.execute(
                select(AuditEventRow.action, AuditEventRow.object_ref)
                .where(AuditEventRow.action.like("controller_member.%"))
                .order_by(AuditEventRow.at)
            )
        ).all()
    assert [a for a, _ in rows] == [
        "controller_member.created",
        "controller_member.updated",
        "controller_member.deleted",
    ]
    assert {o for _, o in rows} == {"controller_member:ctl-esxi"}


EXAMPLE = Path(__file__).parents[3] / "deploy/prod/adopt/management.example.yaml"


def test_the_adoption_example_lists_the_external_member() -> None:
    members = load_controller_members(EXAMPLE.read_text("utf-8"))
    assert [(m["name"], m["platform"]) for m in members] == [
        ("ctl-1", "proxmox"),
        ("ctl-2", "proxmox"),
        ("ctl-esxi", "esxi"),
    ]


def test_an_adoption_file_with_a_bad_or_repeated_member_is_refused() -> None:
    text = EXAMPLE.read_text("utf-8")
    data = yaml.safe_load(text)
    data["controller_members"][0]["address"] = "127.0.0.1"
    with pytest.raises(ValueError, match=r"ctl-1.*address"):
        load_controller_members(yaml.safe_dump(data))
    data["controller_members"][0]["address"] = "10.0.0.11"
    data["controller_members"].append(dict(data["controller_members"][0]))
    with pytest.raises(ValueError, match="listed twice"):
        load_controller_members(yaml.safe_dump(data))
    data["controller_members"] = ["nope"]
    with pytest.raises(ValueError, match="mappings"):
        load_controller_members(yaml.safe_dump(data))
    assert load_controller_members("tenants: []") == []


async def test_adopt_records_the_members(pg_url: str) -> None:
    from switchyard.db.tenancy import ADMIN_ROLE, make_engine

    engine = make_engine(pg_url, role=ADMIN_ROLE)
    try:
        await adopt(engine, EXAMPLE)
        async with admin_session(engine) as session:
            rows = list((await session.execute(select(ControllerMemberRow))).scalars())
    finally:
        await engine.dispose()
    by_name = {r.name: r for r in rows}
    assert set(by_name) == {"ctl-1", "ctl-2", "ctl-esxi"}
    assert by_name["ctl-esxi"].external and by_name["ctl-esxi"].nofailover
    assert not by_name["ctl-1"].external
    assert by_name["ctl-esxi"].patroni_url == "http://10.0.0.13:8008"
    assert by_name["ctl-esxi"].created_by == "syctl adopt"
