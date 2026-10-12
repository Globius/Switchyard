"""The HTTP API against a real database (ASGI transport, fake engines)."""

import json
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


async def test_protocol_field_rules_describe_what_a_form_may_offer(api: Api) -> None:
    h = await api.token()
    udp = (await api.client.get(f"{V1}/schema/virtual-service?protocol=udp", headers=h)).json()
    assert "certificate_ids" in udp["service_forbidden"]
    assert {"tls", "proxy_protocol", "http2", "https_redirect_port"} <= set(udp["port_forbidden"])
    assert {"tls", "persistence"} <= set(udp["pool_forbidden"])
    assert "random" not in udp["algorithms"]
    assert (udp["monitor_types"], udp["member_port_required"]) == (["udp"], True)
    http = (await api.client.get(f"{V1}/schema/virtual-service?protocol=http", headers=h)).json()
    assert http["service_forbidden"] == [] and "udp" not in http["monitor_types"]
    bad = await api.client.get(f"{V1}/schema/virtual-service?protocol=sctp", headers=h)
    assert bad.status_code == 422 and bad.json()["errors"][0]["path"] == "protocol"
    assert (await api.client.get(f"{V1}/schema/virtual-service?protocol=udp")).status_code == 401


async def test_udp_service_fields_are_rejected_on_their_own_path(api: Api) -> None:
    h = await api.token()
    cs = (
        await api.client.post(f"{V1}/tenants/acme/change-sets", json={"summary": "wg"}, headers=h)
    ).json()["id"]
    made = await api.client.post(
        f"{V1}/tenants/acme/pools?change_set={cs}",
        json={
            "name": "wg",
            "tls": True,
            "members": [{"id": str(uuid.uuid4()), "address": "10.20.0.50", "port": 51820}],
        },
        headers=h,
    )
    assert made.status_code == 202, made.text
    created = await api.client.post(
        f"{V1}/tenants/acme/virtual-services?change_set={cs}",
        json={
            "name": "wg",
            "se_group_id": str(GROUP),
            "protocol": "udp",
            "vip": "192.0.2.40",
            "ports": [{"port": 51820, "proxy_protocol": True}],
            "default_pool_id": made.json()["object_id"],
        },
        headers=h,
    )
    assert created.status_code == 202, created.text
    diff = (await api.client.get(f"{V1}/tenants/acme/change-sets/{cs}/diff", headers=h)).json()
    found = {(p["path"], p["code"]) for p in diff["problems"]}
    assert ("virtual_services.wg.ports[0].proxy_protocol", "udp_field_not_allowed") in found
    assert ("pools.wg.tls", "udp_field_not_allowed") in found


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
    assert "change_set.applied" in {e["action"] for e in audit["items"]}
    only = (
        await api.client.get(f"{V1}/tenants/acme/audit-events?action=change_set.*", headers=h)
    ).json()
    assert only["items"] and all(e["action"].startswith("change_set.") for e in only["items"])
    first = (await api.client.get(f"{V1}/tenants/acme/audit-events?limit=1", headers=h)).json()
    assert first["next_cursor"]
    nxt = (
        await api.client.get(
            f"{V1}/tenants/acme/audit-events?limit=1&cursor={first['next_cursor']}", headers=h
        )
    ).json()
    assert "items" in nxt, (nxt, first)
    assert nxt["items"] and nxt["items"][0] != first["items"][0]
    none = (await api.client.get(f"{V1}/tenants/acme/audit-events?q=zzz-no-such", headers=h)).json()
    assert none == {"items": [], "next_cursor": None}
    bad = await api.client.get(f"{V1}/tenants/acme/audit-events?cursor=x", headers=h)
    assert bad.status_code == 400


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
    by_token = await api.client.post(
        f"{V1}/tenants/acme/tokens", json={"name": "ci", "role": "operator"}, headers=h
    )
    assert by_token.status_code == 403 and by_token.json()["code"] == "forbidden"
    assert "cannot create tokens" in by_token.json()["detail"]
    token, record = await api.services.tokens.create(ROOT, TENANT, "ci", Role.OPERATOR)
    ok = await api.client.get(
        f"{V1}/tenants/acme/pools", headers={"Authorization": f"Bearer {token}"}
    )
    assert ok.status_code == 200
    above = await api.client.post(
        f"{V1}/tenants/acme/tokens", json={"name": "boss", "role": "system-admin"}, headers=h
    )
    assert above.status_code == 403
    await api.client.delete(f"{V1}/tenants/acme/tokens/{record.id}", headers=h)
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
        f"{t}/status/virtual-services",
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


async def test_the_diff_has_no_warning_for_udp_services_since_m6(api: Api) -> None:
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
    assert diff["warnings"] == []


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
            {
                "kind": "rewrite",
                "name": "edge-headers",
                "rules": [
                    {"action": {"type": "set_header", "name": "X-Edge", "value": "sy"}},
                    {
                        "phase": "response",
                        "match": {"methods": ["GET"], "path_prefix": "/static"},
                        "action": {"type": "remove_header", "name": "Server"},
                    },
                    {
                        "action": {
                            "type": "replace_prefix",
                            "from_prefix": "/api/v1",
                            "to_prefix": "/",
                        }
                    },
                ],
            },
            {
                "kind": "respond",
                "name": "maintenance",
                "rules": [
                    {
                        "status": 503,
                        "content_type": "text/html",
                        "body": "<p>back soon</p>",
                        "retry_after_s": 60,
                    }
                ],
            },
            {
                "kind": "error_pages",
                "name": "pages",
                "pages": [{"status": 503, "body": "<p>down</p>"}],
            },
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
    bad_rewrite = {
        "kind": "rewrite",
        "name": "bad-rw",
        "rules": [{"action": {"type": "set_header", "name": "Content-Length", "value": "0"}}],
    }
    r = await api.client.post(f"{t}/policy-sets?apply=true", json=bad_rewrite, headers=h)
    assert r.status_code == 422
    assert any("cannot be changed" in e["message"] for e in r.json()["errors"])
    unsafe = {
        "kind": "rewrite",
        "name": "unsafe",
        "rules": [{"action": {"type": "set_header", "name": "X-A", "value": "%[src]"}}],
    }
    r = await api.client.post(f"{t}/policy-sets?apply=true", json=unsafe, headers=h)
    assert r.status_code == 422
    assert "rules.0.action.value" in {e["path"] for e in r.json()["errors"]}, r.json()


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


async def test_rendered_config_per_object_live_and_draft(api: Api) -> None:
    h = await api.token()
    rendered = f"{V1}/tenants/acme/pools/{POOL}/rendered"
    live = await api.client.get(rendered, headers=h)
    assert live.status_code == 200
    [item] = live.json()["items"]
    assert (item["file"], item["language"], item["se_group_id"]) == (
        "haproxy/haproxy.cfg",
        "haproxy",
        str(GROUP),
    )
    assert len(item["fingerprint"]) == 64 and item["range"][0] <= item["range"][1]

    cs = (
        await api.client.post(f"{V1}/tenants/acme/change-sets", json={"summary": "x"}, headers=h)
    ).json()["id"]
    await api.client.patch(
        f"{V1}/tenants/acme/pools/{POOL}?change_set={cs}",
        json={"algorithm": "source"},
        headers={**h, "If-Match": '"1"'},
    )
    draft = (await api.client.get(f"{rendered}?change_set={cs}", headers=h)).json()["items"][0]
    assert "balance source" in draft["content"] and "balance source" not in item["content"]
    assert draft["fingerprint"] != item["fingerprint"]

    diff = (await api.client.get(f"{V1}/tenants/acme/change-sets/{cs}/diff", headers=h)).json()
    [changed] = diff["se_groups"][0]["files"]
    assert (changed["before"], changed["after"]) == (item["content"], draft["content"])
    assert changed["status"] == "modified" and changed["diff"]

    vs = await api.client.get(f"{V1}/tenants/acme/virtual-services/{VS}/rendered", headers=h)
    assert vs.json()["items"][0]["content"].count("frontend vs_") >= 1
    missing = await api.client.get(f"{V1}/tenants/acme/pools/{uuid.uuid4()}/rendered", headers=h)
    assert missing.status_code == 404
    unsupported = await api.client.get(
        f"{V1}/tenants/acme/networks/{uuid.uuid4()}/rendered", headers=h
    )
    assert unsupported.status_code in (404, 405)


async def test_vip_pool_allocations_and_next_free(api: Api) -> None:
    from switchyard.domain.loader import symbolic_uuid

    h = await api.token(Role.VIEWER)
    pool = symbolic_uuid("vp-main")
    usage = (
        await api.client.get(f"{V1}/tenants/acme/vip-pools/{pool}/allocations", headers=h)
    ).json()
    assert usage["total"] == 41 and usage["used"] == 1 and usage["free"] == 40
    assert usage["allocations"][0]["address"] == "192.0.2.20"
    assert usage["allocations"][0]["virtual_service_id"] == str(VS)
    assert usage["next_free"] == "192.0.2.10"
    nxt = await api.client.post(f"{V1}/tenants/acme/vip-pools/{pool}/next-free", headers=h)
    assert nxt.json() == {"address": "192.0.2.10"}
    missing = await api.client.get(
        f"{V1}/tenants/acme/vip-pools/{uuid.uuid4()}/allocations", headers=h
    )
    assert missing.status_code == 404


async def test_system_audit_engines_and_revision_history(api: Api) -> None:
    from switchyard.domain.loader import symbolic_uuid

    tenant_admin = await api.token()
    root = await api.token(Role.SYSTEM_ADMIN)
    # The cross-tenant log is for system administrators only.
    assert (await api.client.get(f"{V1}/audit-events", headers=tenant_admin)).status_code == 403
    page = (await api.client.get(f"{V1}/audit-events?limit=5", headers=root)).json()
    assert page["items"] and "tenant_id" in page["items"][0]
    scoped = (await api.client.get(f"{V1}/audit-events?tenant=acme&limit=500", headers=root)).json()
    assert all(e["tenant_id"] == str(TENANT) for e in scoped["items"])
    actions = (await api.client.get(f"{V1}/audit-actions", headers=tenant_admin)).json()
    assert {"action": "job.retried", "category": "job"}.items() <= next(
        a for a in actions if a["action"] == "job.retried"
    ).items()
    # Engines: tenant-wide list, one engine, filters.
    made = await api.client.post(
        f"{V1}/tenants/acme/se-groups/{GROUP}/engines",
        json={"name": "e1", "dataplane_url": "https://192.0.2.9:5555", "mgmt_ip": "192.0.2.9"},
        headers=tenant_admin,
    )
    assert made.status_code == 201 and made.json()["agent_enrolled"] is False
    engines = (await api.client.get(f"{V1}/tenants/acme/engines", headers=tenant_admin)).json()
    assert engines and {"pve_node", "vmid", "agent_enrolled", "se_group_id"} <= engines[0].keys()
    one = await api.client.get(
        f"{V1}/tenants/acme/engines/{engines[0]['id']}", headers=tenant_admin
    )
    assert one.json()["name"] == engines[0]["name"]
    none = (
        await api.client.get(f"{V1}/tenants/acme/engines?state=disabled", headers=tenant_admin)
    ).json()
    assert none == []
    assert (
        await api.client.get(f"{V1}/tenants/acme/engines/{uuid.uuid4()}", headers=tenant_admin)
    ).status_code == 404
    # Revision history of one object.
    pool = symbolic_uuid("pool-web")
    h = tenant_admin
    r = await api.client.patch(
        f"{V1}/tenants/acme/pools/{pool}?apply=true",
        json={"tls": True},
        headers={**h, "If-Match": '"1"'},
    )
    assert r.status_code == 202
    await api.run_jobs()
    hist = (await api.client.get(f"{V1}/tenants/acme/revisions?object={pool}", headers=h)).json()
    assert len(hist["items"]) == 1
    other = (
        await api.client.get(f"{V1}/tenants/acme/revisions?object={uuid.uuid4()}", headers=h)
    ).json()
    assert other["items"] == []
    states = (await api.client.get(f"{V1}/tenants/acme/status/virtual-services", headers=h)).json()
    assert all(
        s["health"] in {"healthy", "degraded", "down", "unknown", "disabled"} for s in states
    )


async def test_search_and_summary(api: Api) -> None:
    h = await api.token(Role.VIEWER)
    found = (await api.client.get(f"{V1}/tenants/acme/search?q=WEB", headers=h)).json()
    assert found["hits"] and found["hits"][0]["name"].lower().startswith("web")
    by_vip = (await api.client.get(f"{V1}/tenants/acme/search?q=192.0.2.20", headers=h)).json()
    assert [x["matched"] for x in by_vip["hits"]][:1] == ["vip"]
    only = (
        await api.client.get(f"{V1}/tenants/acme/search?q=web&types=pool&limit=1", headers=h)
    ).json()
    assert {x["type"] for x in only["hits"]} <= {"pool"}
    assert (await api.client.get(f"{V1}/tenants/acme/search?q=", headers=h)).status_code == 422
    summary = (await api.client.get(f"{V1}/tenants/acme/summary", headers=h)).json()
    assert summary["services"]["total"] >= 1
    assert {"jobs", "open_drafts", "latest_revision", "expiring_certificates"} <= summary.keys()


async def test_list_filters_and_sort(api: Api) -> None:
    h = await api.token(Role.VIEWER)
    base = f"{V1}/tenants/acme/virtual-services"
    everything = (await api.client.get(base, headers=h)).json()["items"]
    assert everything
    names = [o["name"] for o in everything]
    desc = (await api.client.get(f"{base}?sort=-name", headers=h)).json()["items"]
    assert [o["name"] for o in desc] == sorted(names, reverse=True)
    found = (await api.client.get(f"{base}?q={names[0][:2].upper()}", headers=h)).json()["items"]
    assert names[0] in {o["name"] for o in found}
    none = (await api.client.get(f"{base}?protocol=udp&enabled=false", headers=h)).json()
    assert none["items"] == []
    by_group = (await api.client.get(f"{base}?se_group={GROUP}", headers=h)).json()["items"]
    assert len(by_group) == len(everything)
    assert (await api.client.get(f"{base}?sort=color", headers=h)).status_code == 422
    certs = f"{V1}/tenants/acme/certificates"
    soon = (await api.client.get(f"{certs}?expires_within_days=0", headers=h)).json()
    assert isinstance(soon["items"], list)
    pools = (await api.client.get(f"{V1}/tenants/acme/pools?q=zzz", headers=h)).json()
    assert pools["items"] == []


async def test_stale_operation_reports_the_conflict_and_the_diff_has_impact(api: Api) -> None:
    h = await api.token()
    cs = (
        await api.client.post(f"{V1}/tenants/acme/change-sets", json={"summary": "x"}, headers=h)
    ).json()["id"]
    stale = await api.client.post(
        f"{V1}/tenants/acme/change-sets/{cs}/ops",
        json={
            "ops": [
                {
                    "object_type": "pool",
                    "object_id": str(POOL),
                    "action": "update",
                    "body": {"algorithm": "leastconn"},
                    "expected_version": 99,
                }
            ]
        },
        headers=h,
    )
    assert stale.status_code == 412
    conflict = stale.json()["conflict"]
    assert conflict["object_type"] == "pool" and conflict["expected_version"] == 99
    assert conflict["current_version"] == 1
    ok = await api.client.post(
        f"{V1}/tenants/acme/change-sets/{cs}/ops",
        json={
            "ops": [
                {
                    "object_type": "pool",
                    "object_id": str(POOL),
                    "action": "update",
                    "body": {"algorithm": "leastconn"},
                    "expected_version": conflict["current_version"],
                }
            ]
        },
        headers=h,
    )
    assert ok.status_code == 200
    diff = (await api.client.get(f"{V1}/tenants/acme/change-sets/{cs}/diff", headers=h)).json()
    assert [s["id"] for s in diff["impact"]["services"]] == [str(VS)]
    assert diff["impact"]["vips"] and diff["impact"]["se_groups"][0]["name"]


async def test_metrics_and_logs_are_tenant_scoped_and_degrade_when_unconfigured(api: Api) -> None:
    from datetime import UTC, datetime

    from switchyard.adapters.telemetry import FakeTelemetry
    from switchyard.domain.telemetry import AccessLogEntry

    h = await api.token(Role.VIEWER)
    base = f"{V1}/tenants/acme"
    off = (await api.client.get(f"{base}/virtual-services/{VS}/metrics", headers=h)).json()
    assert off["available"] is False and off["reason"]
    assert (await api.client.get(f"{base}/virtual-services/{VS}/logs", headers=h)).json()[
        "entries"
    ] == []
    fake = FakeTelemetry()
    fake.entries = [
        AccessLogEntry(
            at=datetime.now(UTC),
            client_ip="192.0.2.1",
            frontend="vs_web",
            backend="pool_web",
            server="s",
            status=502,
            bytes=1,
            duration_ms=1,
            raw="x",
        )
    ]
    api.services.telemetry = fake
    on = (await api.client.get(f"{base}/virtual-services/{VS}/metrics", headers=h)).json()
    assert on["available"] and on["metrics"]["summary"]["rps"] == 2.0
    over = (await api.client.get(f"{base}/metrics/virtual-services", headers=h)).json()
    assert over["available"] and over["items"]
    logs = (
        await api.client.get(f"{base}/virtual-services/{VS}/logs?status_class=5", headers=h)
    ).json()
    assert [e["status"] for e in logs["entries"]] == [502]
    none = (
        await api.client.get(f"{base}/virtual-services/{VS}/logs?status_class=2", headers=h)
    ).json()
    assert none["entries"] == []
    missing = await api.client.get(f"{base}/virtual-services/{uuid.uuid4()}/metrics", headers=h)
    assert missing.status_code == 404
    assert set(fake.asked) == {TENANT}


async def test_engine_metrics_are_tenant_scoped_ranged_and_degrade(api: Api) -> None:
    from datetime import UTC, datetime, timedelta

    from switchyard.adapters.telemetry import FakeTelemetry

    h = await api.token(Role.VIEWER)
    admin = await api.token()
    base = f"{V1}/tenants/acme"
    engine = (
        await api.client.post(
            f"{base}/se-groups/{GROUP}/engines",
            json={"name": "se-metrics-1", "dataplane_url": "https://198.51.100.9:5555"},
            headers=admin,
        )
    ).json()
    url = f"{base}/engines/{engine['id']}/metrics"
    off = (await api.client.get(url, headers=h)).json()
    assert off["available"] is False and off["reason"]
    fake = FakeTelemetry()
    api.services.telemetry = fake
    end = datetime.now(UTC) - timedelta(hours=2)
    on = await api.client.get(url, params={"window_s": 7200, "until": end.isoformat()}, headers=h)
    body = on.json()
    assert on.status_code == 200 and body["available"]
    assert body["metrics"]["summary"]["vcpus"] == 4 and body["metrics"]["window_s"] == 7200
    assert fake.engines == ["se-metrics-1"] and set(fake.asked) == {TENANT}
    assert fake.until is not None and abs((fake.until - end).total_seconds()) < 1
    future = datetime.now(UTC) + timedelta(hours=1)
    assert (
        await api.client.get(url, params={"until": future.isoformat()}, headers=h)
    ).status_code == 422
    assert (await api.client.get(url, params={"window_s": 5}, headers=h)).status_code == 422
    unknown = await api.client.get(f"{base}/engines/{uuid.uuid4()}/metrics", headers=h)
    assert unknown.status_code == 404
    # a tenant that does not own the engine cannot read it
    globex = (await api.services.tenants.create(ROOT, "globex-metrics")).id
    other = await api.token(Role.VIEWER, tenant=globex)
    foreign = f"{V1}/tenants/globex-metrics/engines/{engine['id']}/metrics"
    assert (await api.client.get(foreign, headers=other)).status_code == 404


async def test_monitor_test_runs_from_an_engine_through_the_agent(api: Api) -> None:
    from switchyard.adapters.agent import FakeAgent, ProbeResult

    h = await api.token()
    base = f"{V1}/tenants/acme"
    engine = (
        await api.client.post(
            f"{base}/se-groups/{GROUP}/engines",
            json={
                "name": "e9",
                "dataplane_url": "https://192.0.2.9:5555",
                "mgmt_ip": "192.0.2.9",
                "agent_url": "192.0.2.9:7443",
            },
            headers=h,
        )
    ).json()
    made = await api.client.post(
        f"{base}/health-monitors?apply=true",
        json={"name": "tcp9", "type": "tcp"},
        headers=h,
    )
    assert made.status_code == 202, made.text
    await api.run_jobs()
    monitors = (await api.client.get(f"{base}/health-monitors", headers=h)).json()["items"]
    monitor = next(m["id"] for m in monitors if m["name"] == "tcp9")
    pool = (await api.client.get(f"{base}/pools/{POOL}", headers=h)).json()
    member = pool["members"][0]["id"]
    body = {"pool_id": str(POOL), "member_id": member}
    url = f"{base}/health-monitors/{monitor}/test"
    # No agent connected yet: the engine cannot be reached.
    first = await api.client.post(url, json=body, headers=h)
    assert first.status_code == 409, first.text
    agent = FakeAgent("e9")
    from switchyard.pipeline.engines import StaticEngineDirectory

    api.services.monitor_tests._directory = StaticEngineDirectory(
        {}, {uuid.UUID(engine["id"]): agent}
    )
    ok = await api.client.post(url, json=body, headers=h)
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True and ok.json()["engine"] == "e9"
    assert (agent.probes[0].address, agent.probes[0].port) == ("10.20.0.11", 8080)
    agent.probe_result = ProbeResult(ok=False, latency_ms=0, detail="x", refused=True)
    assert (await api.client.post(url, json=body, headers=h)).status_code == 409
    viewer = await api.token(Role.VIEWER)
    assert (await api.client.post(url, json=body, headers=viewer)).status_code == 403


async def test_audit_export_streams_every_event_as_csv_or_jsonl(api: Api) -> None:
    import csv
    import io
    import json

    h = await api.token()
    root = await api.token(Role.SYSTEM_ADMIN)
    page = (await api.client.get(f"{V1}/tenants/acme/audit-events?limit=500", headers=h)).json()
    assert page["items"]
    csv_text = (await api.client.get(f"{V1}/tenants/acme/audit-events/export", headers=h)).text
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert list(rows[0]) == ["at", "tenant_id", "actor", "action", "object", "request_id", "diff"]
    assert len(rows) >= len(page["items"])
    lines = (
        await api.client.get(f"{V1}/tenants/acme/audit-events/export?format=jsonl", headers=h)
    ).text.splitlines()
    assert {json.loads(line)["action"] for line in lines} >= {"audit.export"}
    filtered = await api.client.get(
        f"{V1}/tenants/acme/audit-events/export?format=jsonl&action=audit.*", headers=h
    )
    assert {json.loads(x)["action"] for x in filtered.text.splitlines()} == {"audit.export"}
    assert filtered.headers["content-disposition"] == 'attachment; filename="audit-acme.jsonl"'
    bad = await api.client.get(f"{V1}/tenants/acme/audit-events/export?format=xml", headers=h)
    assert bad.status_code == 422
    assert (await api.client.get(f"{V1}/audit-events/export", headers=h)).status_code == 403
    everything = await api.client.get(f"{V1}/audit-events/export?format=jsonl", headers=root)
    assert everything.status_code == 200 and everything.text.strip()


async def test_audit_export_pages_through_more_than_one_page(api: Api) -> None:
    import json

    from switchyard.api import app as api_app

    h = await api.token()
    for _ in range(5):
        await api.client.get(f"{V1}/tenants/acme/audit-events/export", headers=h)  # 5 export events
    original = api_app.EXPORT_PAGE
    api_app.EXPORT_PAGE = 2
    try:
        text = (
            await api.client.get(f"{V1}/tenants/acme/audit-events/export?format=jsonl", headers=h)
        ).text
    finally:
        api_app.EXPORT_PAGE = original
    events = [json.loads(x) for x in text.splitlines()]
    total = (await api.client.get(f"{V1}/tenants/acme/audit-events?limit=500", headers=h)).json()
    assert len(events) >= 5 and len({(e["at"], e["request_id"], e["action"]) for e in events}) > 2
    assert [e["at"] for e in events] == sorted((e["at"] for e in events), reverse=True)
    assert len(events) <= len(total["items"]) + 1


async def test_tenant_read_reports_usage_and_quotas_block_growth(api: Api) -> None:
    root = await api.token(Role.SYSTEM_ADMIN)
    before = (await api.client.get(f"{V1}/tenants/acme", headers=root)).json()
    pools = before["usage"]["pool"]
    assert pools >= 1 and before["usage"]["virtual_service"] >= 1
    bad = await api.client.patch(
        f"{V1}/tenants/acme", json={"quotas": {"nope": 1}}, headers={**root, "If-Match": '"1"'}
    )
    assert bad.status_code == 422
    ok = await api.client.patch(
        f"{V1}/tenants/acme",
        json={"quotas": {"pool": pools}},
        headers={**root, "If-Match": f'"{before["version"]}"'},
    )
    assert ok.status_code == 200 and ok.json()["usage"]["pool"] == pools
    h = await api.token()
    refused = await api.client.post(
        f"{V1}/tenants/acme/pools?apply=true",
        json={"name": "one-too-many", "members": []},
        headers=h,
    )
    assert refused.status_code == 422
    assert "quota_exceeded" in refused.text


async def test_reapply_endpoint_queues_a_job_the_worker_runs(api: Api) -> None:
    h = await api.token()
    wanted = api.w.engines["se-a"].files["haproxy/haproxy.cfg"]
    api.w.engines["se-a"].files["haproxy/haproxy.cfg"] = "# drifted\n" + wanted
    viewer = await api.token(Role.OPERATOR)
    assert (
        await api.client.post(f"{V1}/tenants/acme/se-groups/{GROUP}/reapply", headers=viewer)
    ).status_code == 403
    missing = await api.client.post(
        f"{V1}/tenants/acme/se-groups/{uuid.uuid4()}/reapply", headers=h
    )
    assert missing.status_code == 404
    queued = await api.client.post(f"{V1}/tenants/acme/se-groups/{GROUP}/reapply", headers=h)
    assert queued.status_code == 202
    await api.run_jobs()
    job = (
        await api.client.get(f"{V1}/tenants/acme/jobs/{queued.json()['job_id']}", headers=h)
    ).json()
    assert job["kind"] == "reapply_se_group" and job["state"] == "succeeded"
    assert job["ok"] is True and job["result"]["revision_number"] == 0
    assert api.w.engines["se-a"].files["haproxy/haproxy.cfg"] == wanted


async def test_engine_credentials_cannot_point_at_another_tenants_secret(api: Api) -> None:
    h = await api.token()
    body = {"name": "e9", "dataplane_url": "https://192.0.2.9:5555", "mgmt_ip": "192.0.2.9"}
    url = f"{V1}/tenants/acme/se-groups/{GROUP}/engines"
    for ref in ("kv/tenants/globex/engines/dataplane", "kv/proxmox/token"):
        r = await api.client.post(url, json=body | {"credentials_ref": ref}, headers=h)
        assert r.status_code == 422, r.text
    own = await api.client.post(
        url, json=body | {"credentials_ref": "kv/tenants/acme/engines/dataplane"}, headers=h
    )
    assert own.status_code == 201


async def test_engine_addresses_count_as_used_in_a_vip_pool(api: Api) -> None:
    from sqlalchemy import text

    from switchyard.db.tenancy import admin_session
    from switchyard.domain.loader import symbolic_uuid

    async with admin_session(api.w.db.admin) as s:
        await s.execute(
            text(
                "INSERT INTO service_engine (id, tenant_id, se_group_id, name, dataplane_url, "
                "state, mgmt_ip, data_ips) VALUES (gen_random_uuid(), :t, :g, 'held', "
                "'https://x', 'enabled', '192.0.2.11', CAST(:d AS jsonb))"
            ),
            {
                "t": TENANT,
                "g": GROUP,
                "d": json.dumps([{"network_id": symbolic_uuid("net-lb"), "address": "192.0.2.10"}]),
            },
        )
    h = await api.token(Role.VIEWER)
    pool = symbolic_uuid("vp-main")
    url = f"{V1}/tenants/acme/vip-pools/{pool}"
    usage = (await api.client.get(f"{url}/allocations", headers=h)).json()
    assert usage["engine_addresses"] == ["192.0.2.10", "192.0.2.11"]
    assert usage["used"] == 3 and usage["free"] == 38
    assert usage["next_free"] == "192.0.2.12"
    assert (await api.client.post(f"{url}/next-free", headers=h)).json() == {
        "address": "192.0.2.12"
    }
