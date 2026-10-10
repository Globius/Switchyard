"""The HTTP API against a real database (ASGI transport, fake engines)."""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.domain.workflow import Role
from switchyard.jobs.handlers import build_handlers
from switchyard.jobs.worker import Worker
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from tests.pipeline_env import GROUP, POOL, TENANT, VS, World, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)


class Api:
    def __init__(self, w: World, services: ApiServices, client: httpx.AsyncClient) -> None:
        self.w, self.services, self.client = w, services, client

    async def token(
        self, role: Role = Role.TENANT_ADMIN, tenant: uuid.UUID | None = TENANT
    ) -> dict[str, str]:
        name = f"t{uuid.uuid4().hex[:8]}"
        if role is Role.SYSTEM_ADMIN:
            tenant = None
        plaintext, _ = await self.services.tokens.create(ROOT, tenant, name, role)
        return {"Authorization": f"Bearer {plaintext}"}

    async def run_jobs(self) -> None:
        worker = Worker(
            self.w.db.admin, build_handlers(self.w.db, self.w.pipeline, None), worker_id="api"
        )
        while await worker.run_once():
            pass


@pytest_asyncio.fixture
async def api(pg_url: str) -> AsyncIterator[Api]:
    async with world(pg_url) as w:
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        services = build_services(w.db, directory)
        app = create_app(services)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            yield Api(w, services, client)


V1 = "/api/v1"


async def test_authentication_is_required_and_errors_are_problem_json(api: Api) -> None:
    r = await api.client.get(f"{V1}/tenants")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.headers["www-authenticate"] == "Bearer"
    bad = await api.client.get(f"{V1}/tenants", headers={"Authorization": "Bearer sy_nope"})
    assert bad.status_code == 401
    assert (await api.client.get(f"{V1}/healthz")).json() == {"status": "ok"}


async def test_request_id_is_echoed(api: Api) -> None:
    r = await api.client.get(f"{V1}/healthz", headers={"X-Request-ID": "abc"})
    assert r.headers["x-request-id"] == "abc"
    assert (await api.client.get(f"{V1}/healthz")).headers["x-request-id"]


async def test_tenants_are_invisible_to_other_tenants(api: Api) -> None:
    other = uuid.UUID(int=4242)
    await api.services.tenants.create(ROOT, "globex")
    h = await api.token()
    assert [t["name"] for t in (await api.client.get(f"{V1}/tenants", headers=h)).json()] == [
        "acme"
    ]
    assert (await api.client.get(f"{V1}/tenants/globex", headers=h)).status_code == 404
    assert (await api.client.get(f"{V1}/tenants/{other}", headers=h)).status_code == 404
    assert (await api.client.get(f"{V1}/tenants/acme", headers=h)).status_code == 200
    root = await api.token(Role.SYSTEM_ADMIN)
    assert len((await api.client.get(f"{V1}/tenants", headers=root)).json()) == 2
    made = await api.client.post(f"{V1}/tenants", json={"name": "initech"}, headers=h)
    assert made.status_code == 403


async def test_reading_objects_with_etag_filters_and_paging(api: Api) -> None:
    h = await api.token(Role.VIEWER)
    listing = (await api.client.get(f"{V1}/tenants/acme/pools", headers=h)).json()
    assert [p["name"] for p in listing["items"]] == ["web"]
    one = await api.client.get(f"{V1}/tenants/acme/pools/{POOL}", headers=h)
    assert one.headers["etag"] == '"1"'
    assert one.json()["draft_status"] == "unchanged"
    named = (await api.client.get(f"{V1}/tenants/acme/pools?name=nope", headers=h)).json()
    assert named["items"] == []
    assert (
        await api.client.get(f"{V1}/tenants/acme/pools/{uuid.uuid4()}", headers=h)
    ).status_code == 404


async def test_writes_need_a_change_set_or_apply(api: Api) -> None:
    h = await api.token()
    r = await api.client.patch(
        f"{V1}/tenants/acme/pools/{POOL}", json={"tls": True}, headers={**h, "If-Match": '"1"'}
    )
    assert r.status_code == 400
    r = await api.client.patch(
        f"{V1}/tenants/acme/pools/{POOL}?apply=true", json={"tls": True}, headers=h
    )
    assert r.status_code == 428


async def test_validation_errors_point_at_the_field(api: Api) -> None:
    h = await api.token()
    r = await api.client.post(
        f"{V1}/tenants/acme/pools?apply=true", json={"name": "Bad Name", "bogus": 1}, headers=h
    )
    assert r.status_code == 422
    paths = {e["path"] for e in r.json()["errors"]}
    assert {"name", "bogus"} <= paths


async def test_draft_flow_over_http(api: Api) -> None:
    h = await api.token()
    opened = await api.client.post(
        f"{V1}/tenants/acme/change-sets", json={"summary": "tls on"}, headers=h
    )
    cs = opened.json()["id"]
    added = await api.client.patch(
        f"{V1}/tenants/acme/pools/{POOL}?change_set={cs}",
        json={"tls": True},
        headers={**h, "If-Match": '"1"'},
    )
    assert added.status_code == 202
    assert added.json()["op_index"] == 0

    draft = (await api.client.get(f"{V1}/tenants/acme/pools?change_set={cs}", headers=h)).json()
    item = draft["items"][0]
    assert (item["draft_status"], item["changed_fields"], item["tls"]) == (
        "modified",
        ["tls"],
        True,
    )
    live = (await api.client.get(f"{V1}/tenants/acme/pools", headers=h)).json()["items"][0]
    assert live["tls"] is False

    diff = (await api.client.get(f"{V1}/tenants/acme/change-sets/{cs}/diff", headers=h)).json()
    assert diff["objects"][0]["status"] == "modified"
    assert diff["se_groups"][0]["files"], "the rendered configuration changes"

    stale = await api.client.patch(
        f"{V1}/tenants/acme/virtual-services/{VS}?change_set={cs}",
        json={"enabled": True},
        headers={**h, "If-Match": '"7"'},
    )
    assert stale.status_code == 412

    applied = await api.client.post(f"{V1}/tenants/acme/change-sets/{cs}/apply", headers=h)
    assert applied.status_code == 202
    await api.run_jobs()
    job = (await api.client.get(f"{V1}/jobs/{applied.json()['job_id']}", headers=h)).json()
    assert (job["state"], job["result"]["ok"]) == ("succeeded", True)
    assert (await api.client.get(f"{V1}/tenants/acme/pools/{POOL}", headers=h)).json()[
        "tls"
    ] is True
    revs = (await api.client.get(f"{V1}/tenants/acme/revisions", headers=h)).json()
    assert [r["number"] for r in revs] == [1]
    audit = (await api.client.get(f"{V1}/tenants/acme/audit-events", headers=h)).json()
    assert "change_set.applied" in {e["action"] for e in audit}


async def test_apply_true_creates_deletes_and_rolls_back(api: Api) -> None:
    h = await api.token()
    created = await api.client.post(
        f"{V1}/tenants/acme/pools?apply=true",
        json={
            "name": "extra",
            "members": [{"id": str(uuid.uuid4()), "address": "10.20.0.70", "port": 80}],
        },
        headers=h,
    )
    assert created.status_code == 202, created.text
    await api.run_jobs()
    pools = (await api.client.get(f"{V1}/tenants/acme/pools", headers=h)).json()["items"]
    extra = next(p for p in pools if p["name"] == "extra")

    gone = await api.client.delete(
        f"{V1}/tenants/acme/pools/{extra['id']}?apply=true",
        headers={**h, "If-Match": f'"{extra["version"]}"'},
    )
    assert gone.status_code == 202
    await api.run_jobs()
    assert [
        p["name"]
        for p in (await api.client.get(f"{V1}/tenants/acme/pools", headers=h)).json()["items"]
    ] == ["web"]

    back = await api.client.post(f"{V1}/tenants/acme/revisions/1/rollback", headers=h)
    assert back.status_code == 201
    assert back.json()["summary"] == "Roll back to revision 1"
    assert back.json()["ops"][0]["action"] == "create"


async def test_roles_are_enforced(api: Api) -> None:
    viewer = await api.token(Role.VIEWER)
    r = await api.client.post(f"{V1}/tenants/acme/change-sets", json={}, headers=viewer)
    assert r.status_code == 403
    assert r.headers["content-type"].startswith("application/problem+json")
    operator = await api.token(Role.OPERATOR)
    net = await api.client.post(
        f"{V1}/tenants/acme/networks?apply=true",
        json={"name": "n2", "cidr": "198.51.100.0/24"},
        headers=operator,
    )
    assert net.status_code == 403


async def test_member_state_and_engines_over_http(api: Api) -> None:
    h = await api.token()
    pool = (await api.client.get(f"{V1}/tenants/acme/pools/{POOL}", headers=h)).json()
    member = pool["members"][0]["id"]
    r = await api.client.put(
        f"{V1}/tenants/acme/pools/{POOL}/members/{member}/state", json={"state": "drain"}, headers=h
    )
    assert r.status_code == 200
    assert r.json()["applied"] == ["se-a", "se-b"]
    added = await api.client.post(
        f"{V1}/tenants/acme/se-groups/{GROUP}/engines",
        json={"name": "se-c", "dataplane_url": "https://10.0.0.9:5556"},
        headers=h,
    )
    assert added.status_code == 201
    listing = (
        await api.client.get(f"{V1}/tenants/acme/se-groups/{GROUP}/engines", headers=h)
    ).json()
    assert [e["name"] for e in listing] == ["se-c"]


async def test_tokens_can_be_issued_and_revoked(api: Api) -> None:
    h = await api.token()
    made = await api.client.post(
        f"{V1}/tenants/acme/tokens", json={"name": "ci", "role": "operator"}, headers=h
    )
    assert made.status_code == 201
    token = made.json()["token"]
    ok = await api.client.get(
        f"{V1}/tenants/acme/pools", headers={"Authorization": f"Bearer {token}"}
    )
    assert ok.status_code == 200
    above = await api.client.post(
        f"{V1}/tenants/acme/tokens", json={"name": "boss", "role": "system-admin"}, headers=h
    )
    assert above.status_code in {403, 422}
    await api.client.delete(f"{V1}/tenants/acme/tokens/{made.json()['id']}", headers=h)
    again = await api.client.get(
        f"{V1}/tenants/acme/pools", headers={"Authorization": f"Bearer {token}"}
    )
    assert again.status_code == 401


def test_openapi_is_the_contract() -> None:
    from switchyard.api.app import create_app as make

    spec: dict[str, Any] = make(None).openapi()  # type: ignore[arg-type]
    paths = spec["paths"]
    assert "/api/v1/tenants/{t}/virtual-services" in paths
    assert set(paths["/api/v1/tenants/{t}/virtual-services/{object_id}"]) == {
        "get",
        "patch",
        "delete",
    }
    assert VS
