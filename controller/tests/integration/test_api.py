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
    assert [r["number"] for r in revs["items"]] == [1]
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


<<<<<<< HEAD
async def test_every_read_route_answers_with_its_declared_model(api: Api) -> None:
    """Each response is validated against its model by the framework; a mismatch is a 500."""
    h = await api.token()
    t = f"{V1}/tenants/acme"
    for slug in (
        "networks",
        "vip-pools",
        "se-groups",
        "virtual-services",
        "pools",
        "health-monitors",
        "certificates",
        "policy-sets",
        "routing-profiles",
    ):
        r = await api.client.get(f"{t}/{slug}", headers=h)
        assert r.status_code == 200, (slug, r.text)
        for item in r.json()["items"]:
            one = await api.client.get(f"{t}/{slug}/{item['id']}", headers=h)
            assert one.status_code == 200 and one.headers["etag"] == f'"{item["version"]}"', slug
    group = (await api.client.get(f"{t}/se-groups", headers=h)).json()["items"][0]["id"]
    urls = [
        f"{V1}/healthz",
        f"{V1}/tenants",
        t,
        f"{t}/change-sets",
        f"{t}/revisions",
        f"{t}/status/engines",
        f"{t}/audit-events",
        f"{t}/tokens",
        f"{t}/se-groups/{group}/engines",
    ]
    for url in urls:
        assert (await api.client.get(url, headers=h)).status_code == 200, url


async def test_a_body_that_does_not_match_its_model_fails_loudly(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: Any, **__: Any) -> list[Any]:
        return [
            type(
                "R",
                (),
                {
                    "id": uuid.uuid4(),
                    "name": 5,
                    "role": Role.VIEWER,
                    "revoked_at": None,
                    "expires_at": None,
                },
            )()
        ]

    monkeypatch.setattr(api.services.tokens, "list_tokens", broken)
    h = await api.token()
    with pytest.raises(Exception, match="validation"):
        await api.client.get(f"{V1}/tenants/acme/tokens", headers=h)


async def _draft(api: Api, h: dict[str, str], summary: str) -> str:
    opened = await api.client.post(
        f"{V1}/tenants/acme/change-sets", json={"summary": summary}, headers=h
    )
    return str(opened.json()["id"])


async def test_change_sets_and_revisions_page_with_cursors(api: Api) -> None:
    h = await api.token()
    t = f"{V1}/tenants/acme"
    ids = [await _draft(api, h, f"draft {i}") for i in range(3)]
    first = (await api.client.get(f"{t}/change-sets?limit=2", headers=h)).json()
    assert [c["summary"] for c in first["items"]] == ["draft 2", "draft 1"]
    assert first["items"][0]["op_count"] == 0 and first["items"][0]["base_revision"] == 0
    assert first["items"][0]["created_at"]
    rest = (
        await api.client.get(f"{t}/change-sets?limit=2&cursor={first['next_cursor']}", headers=h)
    ).json()
    assert [c["id"] for c in rest["items"]] == [ids[0]] and rest["next_cursor"] is None
    assert (await api.client.get(f"{t}/change-sets?cursor=zzz", headers=h)).status_code == 400

    for i, cs in enumerate(ids[:2]):
        added = await api.client.patch(
            f"{t}/pools/{POOL}?change_set={cs}",
            json={"tls": i == 0},
            headers={**h, "If-Match": '"1"'},
        )
        assert added.status_code == 202, added.text
    counted = (await api.client.get(f"{t}/change-sets?state=draft", headers=h)).json()["items"]
    assert {c["summary"]: c["op_count"] for c in counted} == {
        "draft 0": 1,
        "draft 1": 1,
        "draft 2": 0,
    }

    applied = await api.client.post(f"{t}/change-sets/{ids[0]}/apply", headers=h)
    assert applied.status_code == 202
    await api.run_jobs()
    etag = (await api.client.get(f"{t}/pools/{POOL}", headers=h)).headers["etag"]
    again = await api.client.patch(
        f"{t}/pools/{POOL}?apply=true",
        json={"algorithm": "leastconn"},
        headers={**h, "If-Match": etag},
    )
    assert again.status_code == 202, again.text
    await api.run_jobs()
    revs = (await api.client.get(f"{t}/revisions?limit=1", headers=h)).json()
    assert [r["number"] for r in revs["items"]] == [2] and revs["next_cursor"] == "2"
    older = (await api.client.get(f"{t}/revisions?limit=1&cursor=2", headers=h)).json()
    assert [r["number"] for r in older["items"]] == [1] and older["next_cursor"] is None
    detail = (await api.client.get(f"{t}/revisions/1", headers=h)).json()
    assert detail["created_at"] and {p["name"] for p in detail["snapshot"]["pools"]} == {"web"}


async def test_the_diff_lists_warnings_for_udp_services(api: Api) -> None:
    h = await api.token()
    t = f"{V1}/tenants/acme"
    web = (await api.client.get(f"{t}/virtual-services/{VS}", headers=h)).json()
    cs = await _draft(api, h, "dns")
    pool = {
        "name": "dns-pool",
        "members": [{"id": str(uuid.uuid4()), "address": "10.20.0.80", "port": 53}],
    }
    r = await api.client.post(f"{t}/pools?change_set={cs}", json=pool, headers=h)
    assert r.status_code == 202, r.text
    body = {
        "name": "dns",
        "se_group_id": web["se_group_id"],
        "protocol": "udp",
        "vip": web["vip"],
        "ports": [{"port": 5353}],
        "default_pool_id": r.json()["object_id"],
    }
    r = await api.client.post(f"{t}/virtual-services?change_set={cs}", json=body, headers=h)
    assert r.status_code == 202, r.text
    diff = (await api.client.get(f"{t}/change-sets/{cs}/diff", headers=h)).json()
    assert diff["problems"] == []
    assert diff["warnings"] == [
        {
            "code": "udp_not_served",
            "path": "virtual_services.dns",
            "message": "virtual service dns: udp is rendered by the keepalived renderer, "
            "not haproxy.cfg",
        }
    ]


async def test_policy_sets_and_routing_profiles_are_unions_on_kind(api: Api) -> None:
    h = await api.token()
    t = f"{V1}/tenants/acme"
    made: dict[str, list[dict[str, Any]]] = {
        "policy-sets": [
            {
                "kind": "redirect",
                "name": "old-paths",
                "rules": [
                    {
                        "match": {"path_prefix": "/old"},
                        "redirect": {"location": "/new", "code": 308},
                    }
                ],
            },
            {"kind": "allow_list", "name": "office", "cidrs": ["203.0.113.0/24"]},
        ],
        "routing-profiles": [
            {"kind": "vrrp", "name": "vrrp-b", "vrid": 61, "interface": "eth1"},
            {
                "kind": "bgp",
                "name": "bgp-b",
                "asn": 64512,
                "peers": [{"peer_ip": "192.0.2.254", "remote_asn": 64500}],
            },
        ],
    }
    for slug, bodies in made.items():
        for body in bodies:
            r = await api.client.post(f"{t}/{slug}?apply=true", json=body, headers=h)
            assert r.status_code == 202, (slug, r.text)
            await api.run_jobs()
        items = (await api.client.get(f"{t}/{slug}", headers=h)).json()["items"]
        by_name = {i["name"]: i for i in items}
        for body in bodies:
            item = by_name[body["name"]]
            assert item["kind"] == body["kind"] and item["draft_status"] == "unchanged"
    listing = (await api.client.get(f"{t}/policy-sets", headers=h)).json()["items"]
    office = next(i for i in listing if i["name"] == "office")
    assert office["cidrs"] == ["203.0.113.0/24"] and office["managed_by"] is None
    patched = await api.client.patch(
        f"{t}/policy-sets/{office['id']}?apply=true",
        json={"kind": "allow_list", "cidrs": ["203.0.113.0/25"]},
        headers={**h, "If-Match": f'"{office["version"]}"'},
    )
    assert patched.status_code == 202, patched.text


async def test_union_validation_errors_keep_field_paths_without_the_tag(api: Api) -> None:
    h = await api.token()
    t = f"{V1}/tenants/acme"
    bad = {"kind": "vrrp", "name": "vrrp-x", "vrid": 999, "interface": "eth1"}
    r = await api.client.post(f"{t}/routing-profiles?apply=true", json=bad, headers=h)
    assert r.status_code == 422
    assert {e["path"] for e in r.json()["errors"]} == {"vrid"}
    nested = {
        "kind": "redirect",
        "name": "r",
        "rules": [{"match": {"path_prefix": "/a"}, "redirect": {"location": "no-slash"}}],
    }
    r = await api.client.post(f"{t}/policy-sets?apply=true", json=nested, headers=h)
    assert r.status_code == 422
    paths = {e["path"] for e in r.json()["errors"]}
    assert "rules.0.redirect.location" in paths and not any(p.startswith("redirect") for p in paths)
    r = await api.client.post(
        f"{t}/policy-sets?apply=true", json={"name": "no-kind", "cidrs": ["10.0.0.0/8"]}, headers=h
    )
    assert r.status_code == 422
    r = await api.client.post(
        f"{t}/policy-sets?apply=true", json={"kind": "bogus", "name": "x"}, headers=h
    )
    assert r.status_code == 422
=======
async def test_job_endpoints(api: Api) -> None:
    h = await api.token()
    opened = await api.client.post(
        f"{V1}/tenants/acme/change-sets", json={"summary": "x"}, headers=h
    )
    cs = opened.json()["id"]
    await api.client.patch(
        f"{V1}/tenants/acme/pools/{POOL}?change_set={cs}",
        json={"tls": True},
        headers={**h, "If-Match": '"1"'},
    )
    applied = await api.client.post(f"{V1}/tenants/acme/change-sets/{cs}/apply", headers=h)
    job_id = applied.json()["job_id"]

    queued = (await api.client.get(f"{V1}/tenants/acme/jobs/{job_id}", headers=h)).json()
    assert (queued["state"], queued["retrying"], queued["ok"], queued["stages"]) == (
        "queued",
        False,
        None,
        [],
    )
    await api.run_jobs()

    job = (await api.client.get(f"{V1}/tenants/acme/jobs/{job_id}", headers=h)).json()
    assert (job["state"], job["ok"], job["change_set_id"]) == ("succeeded", True, cs)
    names = [s["name"] for s in job["stages"]]
    assert names == ["lock", "render", "validate", "canary", "rollout", "commit"]
    assert job["result"]["groups"][0]["state"] == "succeeded"

    url = f"{V1}/tenants/acme/jobs?state=succeeded&kind=apply_change_set"
    listed = (await api.client.get(url, headers=h)).json()
    assert [j["id"] for j in listed["items"]] == [job_id]
    bad = await api.client.get(f"{V1}/tenants/acme/jobs?cursor=junk", headers=h)
    assert bad.status_code == 400

    log = (await api.client.get(f"{V1}/tenants/acme/jobs/{job_id}/log", headers=h)).json()
    assert log["lines"][0]["message"].startswith("attempt 1")

    assert (
        await api.client.post(f"{V1}/tenants/acme/jobs/{job_id}/cancel", headers=h)
    ).status_code == 409
    old = (await api.client.get(f"{V1}/jobs/{job_id}", headers=h)).json()
    assert old["state"] == "succeeded", "the old endpoint keeps working"
>>>>>>> 35ab6ad (Jobs: stages with timing, tenant job list, log, retry and cancel (T5), ADR 0016)
