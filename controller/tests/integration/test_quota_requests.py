"""Quota requests: a tenant asks, a system administrator decides (ADR 0199)."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.db.ops_models import AuditEventRow
from switchyard.db.portal_models import QuotaRequestRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from tests.pipeline_env import GROUP, TENANT, World, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

V1 = "/api/v1"
ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)


class Rig:
    def __init__(self, w: World, services: ApiServices, client: httpx.AsyncClient) -> None:
        self.w, self.services, self.client = w, services, client

    async def token(self, role: Role, tenant: uuid.UUID | None = TENANT) -> dict[str, str]:
        if role is Role.SYSTEM_ADMIN:
            tenant = None
        plaintext, _ = await self.services.tokens.create(
            ROOT, tenant, f"t{uuid.uuid4().hex[:8]}", role
        )
        return {"Authorization": f"Bearer {plaintext}"}

    async def set_quota(self, **quotas: int) -> None:
        tenant = await self.services.tenants.resolve(ROOT, "acme")
        await self.services.tenants.update(
            ROOT, tenant.id, expected_version=tenant.version, changes={"quotas": quotas}
        )

    async def quotas(self) -> dict[str, int]:
        return (await self.services.tenants.resolve(ROOT, "acme")).quotas

    async def ask(
        self, h: dict[str, str], limit: int = 6, resource: str = "pool", reason: str = "growth"
    ) -> httpx.Response:
        return await self.client.post(
            f"{V1}/tenants/acme/quota-requests",
            json={"resource": resource, "requested_limit": limit, "reason": reason},
            headers=h,
        )


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        services = build_services(w.db, directory)
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            r = Rig(w, services, client)
            await r.set_quota(pool=3)
            yield r


def fields(r: httpx.Response) -> set[str]:
    return {e["path"] for e in r.json()["errors"]}


async def test_a_tenant_admin_files_a_request_and_a_system_admin_grants_part_of_it(
    rig: Rig,
) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    root = await rig.token(Role.SYSTEM_ADMIN)
    made = await rig.ask(admin, 6, reason="Two teams join in November")
    assert made.status_code == 201, made.text
    body = made.json()
    assert body["state"] == "pending" and body["current_limit"] == 3 and body["tenant"] == "acme"

    queue = (await rig.client.get(f"{V1}/quota-requests", headers=root)).json()
    assert [q["id"] for q in queue] == [body["id"]]

    done = await rig.client.post(
        f"{V1}/quota-requests/{body['id']}/approve",
        json={"granted_limit": 4, "note": "4 for now"},
        headers=root,
    )
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "approved" and done.json()["granted_limit"] == 4
    assert done.json()["decided_by"].startswith("token:")
    assert (await rig.quotas())["pool"] == 4
    assert (await rig.client.get(f"{V1}/quota-requests", headers=root)).json() == []

    mine = (await rig.client.get(f"{V1}/tenants/acme/quota-requests", headers=admin)).json()
    assert [(q["state"], q["decision_note"]) for q in mine] == [("approved", "4 for now")]
    tenant = (await rig.client.get(f"{V1}/tenants/acme", headers=admin)).json()
    assert tenant["quotas"]["pool"] == 4

    again = await rig.client.post(
        f"{V1}/quota-requests/{body['id']}/approve", json={}, headers=root
    )
    assert again.status_code == 409
    late = await rig.client.post(
        f"{V1}/tenants/acme/quota-requests/{body['id']}/withdraw", headers=admin
    )
    assert late.status_code == 409


async def test_approving_without_a_grant_gives_what_was_asked(rig: Rig) -> None:
    admin, root = await rig.token(Role.TENANT_ADMIN), await rig.token(Role.SYSTEM_ADMIN)
    rid = (await rig.ask(admin, 9)).json()["id"]
    done = await rig.client.post(f"{V1}/quota-requests/{rid}/approve", json={}, headers=root)
    assert done.json()["granted_limit"] == 9
    assert (await rig.quotas())["pool"] == 9


async def test_a_grant_stays_between_the_current_and_the_requested_limit(rig: Rig) -> None:
    admin, root = await rig.token(Role.TENANT_ADMIN), await rig.token(Role.SYSTEM_ADMIN)
    rid = (await rig.ask(admin, 6)).json()["id"]
    for bad in (2, 7):
        r = await rig.client.post(
            f"{V1}/quota-requests/{rid}/approve", json={"granted_limit": bad}, headers=root
        )
        assert r.status_code == 422 and fields(r) == {"granted_limit"}
    assert (await rig.quotas())["pool"] == 3


async def test_a_denial_needs_a_note_and_leaves_the_quota_alone(rig: Rig) -> None:
    admin, root = await rig.token(Role.TENANT_ADMIN), await rig.token(Role.SYSTEM_ADMIN)
    rid = (await rig.ask(admin, 6)).json()["id"]
    bare = await rig.client.post(f"{V1}/quota-requests/{rid}/deny", json={}, headers=root)
    assert bare.status_code == 422 and fields(bare) == {"note"}
    denied = await rig.client.post(
        f"{V1}/quota-requests/{rid}/deny", json={"note": "Delete unused pools first"}, headers=root
    )
    assert denied.json()["state"] == "denied"
    assert (await rig.quotas())["pool"] == 3
    mine = (
        await rig.client.get(f"{V1}/tenants/acme/quota-requests?state=denied", headers=admin)
    ).json()
    assert mine[0]["decision_note"] == "Delete unused pools first"
    # A denied resource can be asked for again.
    assert (await rig.ask(admin, 8)).status_code == 201


async def test_bad_requests_are_refused_with_the_field(rig: Rig) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    assert fields(await rig.ask(admin, 3)) == {"requested_limit"}
    assert fields(await rig.ask(admin, 5, reason="  ")) == {"reason"}
    assert fields(await rig.ask(admin, 5, resource="network")) == {"resource"}  # no quota on it
    assert fields(await rig.ask(admin, 5, resource="bogus")) == {"resource"}
    assert fields(await rig.ask(admin, 2_000_000)) == {"requested_limit"}
    assert (await rig.client.get(f"{V1}/quota-requests", headers=admin)).status_code == 403


async def test_one_pending_request_per_resource_and_withdrawing_frees_it(rig: Rig) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    first = await rig.ask(admin, 6)
    assert (await rig.ask(admin, 7)).status_code == 409
    gone = await rig.client.post(
        f"{V1}/tenants/acme/quota-requests/{first.json()['id']}/withdraw", headers=admin
    )
    assert gone.json()["state"] == "withdrawn"
    assert (await rig.ask(admin, 7)).status_code == 201
    unknown = await rig.client.post(
        f"{V1}/tenants/acme/quota-requests/{uuid.uuid4()}/withdraw", headers=admin
    )
    assert unknown.status_code == 404


async def test_a_quota_raised_meanwhile_leaves_nothing_to_grant(rig: Rig) -> None:
    admin, root = await rig.token(Role.TENANT_ADMIN), await rig.token(Role.SYSTEM_ADMIN)
    rid = (await rig.ask(admin, 6)).json()["id"]
    await rig.set_quota(pool=10)
    r = await rig.client.post(f"{V1}/quota-requests/{rid}/approve", json={}, headers=root)
    assert r.status_code == 409 and "nothing to grant" in r.json()["detail"]
    await rig.set_quota()  # the limit was removed altogether
    r = await rig.client.post(f"{V1}/quota-requests/{rid}/approve", json={}, headers=root)
    assert r.status_code == 409 and "unlimited" in r.json()["detail"]
    assert (
        await rig.client.post(f"{V1}/quota-requests/{rid}/deny", json={"note": "no"}, headers=root)
    ).status_code == 200


async def test_only_tenant_admins_ask_and_only_system_admins_decide(rig: Rig) -> None:
    viewer = await rig.token(Role.VIEWER)
    operator = await rig.token(Role.OPERATOR)
    admin = await rig.token(Role.TENANT_ADMIN)
    assert (await rig.ask(viewer)).status_code == 403
    assert (await rig.ask(operator)).status_code == 403
    rid = (await rig.ask(admin)).json()["id"]
    # Everybody in the tenant can read the requests.
    for h in (viewer, operator, admin):
        r = await rig.client.get(f"{V1}/tenants/acme/quota-requests", headers=h)
        assert [q["id"] for q in r.json()] == [rid]
    for h in (viewer, operator, admin):
        r = await rig.client.post(f"{V1}/quota-requests/{rid}/approve", json={}, headers=h)
        assert r.status_code == 403
    assert (await rig.client.get(f"{V1}/quota-requests")).status_code == 401
    withdrawn = await rig.client.post(
        f"{V1}/tenants/acme/quota-requests/{rid}/withdraw", headers=viewer
    )
    assert withdrawn.status_code == 403


async def test_tenants_cannot_see_each_others_requests(rig: Rig) -> None:
    other = await rig.services.tenants.create(ROOT, "globex", quotas={"pool": 1})
    mine = await rig.token(Role.TENANT_ADMIN)
    theirs = await rig.token(Role.TENANT_ADMIN, other.id)
    rid = (await rig.ask(mine)).json()["id"]
    assert (
        await rig.client.get(f"{V1}/tenants/acme/quota-requests", headers=theirs)
    ).status_code == 404
    assert (
        await rig.client.get(f"{V1}/tenants/globex/quota-requests", headers=theirs)
    ).json() == []
    stolen = await rig.client.post(
        f"{V1}/tenants/globex/quota-requests/{rid}/withdraw", headers=theirs
    )
    assert stolen.status_code == 404
    # Row-level security holds even when the service layer is bypassed.
    async with tenant_session(rig.w.db.app, [other.id]) as session:
        assert (
            await session.execute(select(func.count()).select_from(QuotaRequestRow))
        ).scalar_one() == 0
    async with tenant_session(rig.w.db.app, [TENANT]) as session:
        assert (
            await session.execute(select(func.count()).select_from(QuotaRequestRow))
        ).scalar_one() == 1


async def test_every_step_is_audited(rig: Rig) -> None:
    admin, root = await rig.token(Role.TENANT_ADMIN), await rig.token(Role.SYSTEM_ADMIN)
    first = (await rig.ask(admin, 6)).json()["id"]
    await rig.client.post(f"{V1}/tenants/acme/quota-requests/{first}/withdraw", headers=admin)
    second = (await rig.ask(admin, 7)).json()["id"]
    await rig.client.post(f"{V1}/quota-requests/{second}/approve", json={}, headers=root)
    third = (await rig.ask(admin, 12)).json()["id"]
    await rig.client.post(f"{V1}/quota-requests/{third}/deny", json={"note": "no"}, headers=root)
    async with admin_session(rig.w.db.admin) as session:
        rows = (
            await session.execute(
                select(AuditEventRow.action, AuditEventRow.diff)
                .where(AuditEventRow.action.like("quota_request.%"))
                .order_by(AuditEventRow.at)
            )
        ).all()
    assert [a for a, _ in rows] == [
        "quota_request.create",
        "quota_request.withdraw",
        "quota_request.create",
        "quota_request.approve",
        "quota_request.create",
        "quota_request.deny",
    ]
    approved: dict[str, Any] = rows[3][1]
    assert approved == {"resource": "pool", "from": 3, "to": 7}
