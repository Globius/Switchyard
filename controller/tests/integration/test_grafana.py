# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Embedded Grafana: tickets, the proxy session, tenant scoping of data source queries (ADR 0085)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from switchyard.adapters.grafana import FakeGrafana
from switchyard.api.app import build_services, create_app
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from switchyard.services.grafana import GrafanaConfig, GrafanaService, grafana_login
from tests.pipeline_env import TENANT, World, world

pytestmark = pytest.mark.usefixtures("owner_engine")

ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
SECRET = "s" * 40
INTERNAL = "http://grafana.internal:3000"
V1 = "/api/v1"


class Upstreams:
    """Records what reached Grafana, Prometheus and Loki and answers 200."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"Set-Cookie": "grafana_session=abc; Path=/"}
        if request.url.host == "grafana.internal":
            headers["Location"] = f"{INTERNAL}/next"
        body = b'{"status": "success", "data": {"result": []}}'
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))


class Env:
    def __init__(
        self,
        w: World,
        client: httpx.AsyncClient,
        grafana: FakeGrafana,
        upstream: Upstreams,
        service: GrafanaService,
        other: uuid.UUID,
        tokens: dict[str, str],
        clock: list[datetime],
    ) -> None:
        self.w, self.client, self.grafana, self.upstream = w, client, grafana, upstream
        self.service, self.other, self.tokens, self.clock = service, other, tokens, clock

    def auth(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}

    async def ticket(
        self, who: str = "viewer", tenant: str = "acme", body: dict[str, Any] | None = None
    ) -> str:
        r = await self.client.post(
            f"{V1}/tenants/{tenant}/grafana/launch", json=body or {}, headers=self.auth(who)
        )
        assert r.status_code == 200, r.text
        return str(r.json()["url"])

    async def session(self, who: str = "viewer", tenant: str = "acme") -> dict[str, str]:
        r = await self.client.get(await self.ticket(who, tenant))
        assert r.status_code == 303, r.text
        return {"sy_grafana": r.cookies["sy_grafana"]}


@pytest_asyncio.fixture
async def env(pg_url: str) -> AsyncIterator[Env]:
    async with world(pg_url) as w:
        clock = [datetime.now(UTC)]
        grafana = FakeGrafana()
        upstream = Upstreams()
        config = GrafanaConfig(
            internal_url=INTERNAL,
            admin_user="admin",
            admin_password="pw",
            ds_secret=SECRET,
            datasource_url="http://api:8000/internal/ds",
            prometheus_url="http://prometheus.internal:9090",
            loki_url="http://loki.internal:3100",
        )
        service = GrafanaService(w.db, config, grafana, clock=lambda: clock[0])
        services = build_services(w.db, StaticEngineDirectory({}), grafana=service)
        other = (await services.tenants.create(ROOT, "globex")).id
        tokens = {
            "viewer": (await services.tokens.create(ROOT, TENANT, "v", Role.VIEWER))[0],
            "globex": (await services.tokens.create(ROOT, other, "g", Role.VIEWER))[0],
            "root": (await services.tokens.create(ROOT, None, "r", Role.SYSTEM_ADMIN))[0],
        }
        app = create_app(
            services,
            grafana_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler)),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as client:
            yield Env(w, client, grafana, upstream, service, other, tokens, clock)


async def test_a_tenant_user_launches_only_their_own_tenant(env: Env) -> None:
    ok = await env.client.post(
        f"{V1}/tenants/acme/grafana/launch", json={}, headers=env.auth("viewer")
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["url"].startswith("/grafana/launch?ticket=sygt_") and body["tenant_id"] == str(
        TENANT
    )
    # another tenant is "not found", the unscoped view is for system administrators
    assert (
        await env.client.post(
            f"{V1}/tenants/globex/grafana/launch", json={}, headers=env.auth("viewer")
        )
    ).status_code == 404
    assert (
        await env.client.post(f"{V1}/grafana/launch", json={}, headers=env.auth("viewer"))
    ).status_code == 403
    assert (
        await env.client.post(f"{V1}/grafana/launch", json={}, headers=env.auth("root"))
    ).json()["tenant_id"] is None
    assert (await env.client.post(f"{V1}/tenants/acme/grafana/launch", json={})).status_code == 401


async def test_the_landing_page_is_checked(env: Env) -> None:
    for bad in ("//evil.example", "https://evil.example", "/api/admin/users", "/d/../../x"):
        r = await env.client.post(
            f"{V1}/tenants/acme/grafana/launch", json={"landing": bad}, headers=env.auth("viewer")
        )
        assert r.status_code in {422, 400}, bad
    r = await env.client.get(
        await env.ticket(body={"landing": "/d/sy-tenant-services?var-service=vs_a"})
    )
    assert r.headers["location"] == "/grafana/d/sy-tenant-services?var-service=vs_a"


async def test_a_ticket_works_once_and_expires(env: Env) -> None:
    url = await env.ticket()
    first = await env.client.get(url)
    assert first.status_code == 303 and first.headers["location"] == "/grafana/"
    cookie = first.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/grafana" in cookie
    again = await env.client.get(url)
    assert again.status_code == 403 and again.json()["code"] == "grafana_ticket"
    late = await env.ticket()
    env.clock[0] += timedelta(seconds=61)
    assert (await env.client.get(late)).status_code == 403
    assert (await env.client.get("/grafana/launch?ticket=nonsense")).status_code == 403


async def test_redeeming_prepares_a_separate_grafana_org_per_tenant(env: Env) -> None:
    await env.session("viewer", "acme")
    await env.client.get(await env.ticket("globex", "globex"))
    org_a, org_b = env.grafana.orgs[f"sy-{TENANT}"], env.grafana.orgs[f"sy-{env.other}"]
    assert org_a != org_b
    spec_a = {s.uid: s for s in env.grafana.datasources[org_a]}
    spec_b = {s.uid: s for s in env.grafana.datasources[org_b]}
    assert spec_a["prometheus"].url == "http://api:8000/internal/ds/prometheus"
    assert spec_a["loki"].url == "http://api:8000/internal/ds/loki"
    # each organization's data sources carry that tenant's token, and only that
    assert env.service.tenant_for_ds_token(spec_a["prometheus"].bearer) == TENANT
    assert env.service.tenant_for_ds_token(spec_b["prometheus"].bearer) == env.other
    assert env.grafana.homes[org_a] == "sy-tenant-services"
    assert env.grafana.dashboards[org_a] == ["sy-tenant-services"]
    # a person is a Viewer in the one organization of the tenant they opened, nowhere else
    login_a = grafana_login("token:v", TENANT)
    assert env.grafana.members[org_a] == {login_a: "Viewer"}
    assert login_a not in env.grafana.members[org_b]


async def test_the_proxy_needs_the_cookie_and_sets_the_identity_itself(env: Env) -> None:
    assert (await env.client.get("/grafana/api/health")).status_code == 401
    cookies = await env.session()
    r = await env.client.get(
        "/grafana/api/health?x=1",
        cookies=cookies,
        headers={
            "X-WEBAUTH-USER": "admin",
            "X-Grafana-Org-Id": "1",
            "Authorization": "Basic YWRtaW46YWRtaW4=",
            "X-Forwarded-For": "6.6.6.6",
            "Cookie": "grafana_session=stolen; sy_grafana=" + cookies["sy_grafana"],
        },
    )
    assert r.status_code == 200
    seen = env.upstream.requests[-1]
    assert str(seen.url) == f"{INTERNAL}/api/health?x=1"
    org = env.grafana.orgs[f"sy-{TENANT}"]
    assert seen.headers["x-webauth-user"] == grafana_login("token:v", TENANT)
    assert seen.headers["x-grafana-org-id"] == str(org)
    assert "authorization" not in seen.headers and "cookie" not in seen.headers
    assert "x-forwarded-for" not in seen.headers
    assert seen.headers["host"] == "api"
    # Grafana's own session cookie never reaches the browser; its redirects stay under /grafana
    assert "grafana_session" not in r.headers.get("set-cookie", "")
    assert r.headers["location"] == "/grafana/next"


async def test_grafana_administration_is_not_reachable(env: Env) -> None:
    cookies = await env.session()
    for path in (
        "api/admin/users",
        "api/orgs",
        "api/users/lookup",
        "api/serviceaccounts/search",
        "logout",
    ):
        r = await env.client.get(f"/grafana/{path}", cookies=cookies)
        assert r.status_code == 403, path
    assert (await env.client.get("/grafana/a/../../x", cookies=cookies)).status_code in {
        400,
        403,
        404,
    }
    before = len(env.upstream.requests)
    assert (
        await env.client.get("/grafana/%2e%2e/api/admin/users", cookies=cookies)
    ).status_code in {
        400,
        403,
        404,
    }
    assert len(env.upstream.requests) == before


async def test_the_session_ends_with_its_expiry_and_with_the_browser_session(env: Env) -> None:
    cookies = await env.session()
    assert (await env.client.get("/grafana/api/health", cookies=cookies)).status_code == 200
    env.clock[0] += timedelta(minutes=31)
    assert (await env.client.get("/grafana/api/health", cookies=cookies)).status_code == 401
    # a grant tied to a browser session dies when that session is revoked
    async with admin_session(env.w.db.admin) as s:
        await s.execute(
            text(
                "INSERT INTO user_session (id, token_hash, csrf_token, subject, name, expires_at) "
                "VALUES (:id, 'h', 'c', 'alice', 'Alice', now() + interval '1 hour')"
            ),
            {"id": (sid := uuid.uuid4())},
        )
    env.clock[0] = datetime.now(UTC)
    launch = await env.service.launch(
        Principal(actor="user:alice", role=Role.VIEWER, tenant_ids=frozenset({TENANT})),
        TENANT,
        user_session_id=sid,
    )
    proxy_session = await env.service.redeem(launch.ticket)
    assert await env.service.authenticate(proxy_session.cookie) is not None
    async with admin_session(env.w.db.admin) as s:
        await s.execute(text("UPDATE user_session SET revoked_at = now()"))
    assert await env.service.authenticate(proxy_session.cookie) is None


async def test_an_open_tab_keeps_its_session_alive_but_only_for_eight_hours(env: Env) -> None:
    cookies = await env.session()
    fresh = await env.client.get("/grafana/api/health", cookies=cookies)
    assert fresh.status_code == 200 and "sy_grafana" not in fresh.headers.get("set-cookie", "")
    alive_for = timedelta()
    for _ in range(40):  # one request every 20 minutes
        env.clock[0] += timedelta(minutes=20)
        r = await env.client.get("/grafana/api/health", cookies=cookies)
        if r.status_code == 401:
            break
        assert r.status_code == 200
        # the cookie is sent again with the new end, so the browser keeps it as long
        assert "sy_grafana=" in r.headers["set-cookie"]
        alive_for += timedelta(minutes=20)
    assert timedelta(hours=7) < alive_for <= timedelta(hours=8)
    # a quiet tab is not extended: 30 minutes without a request ends the session
    again = await env.session()
    env.clock[0] += timedelta(minutes=31)
    assert (await env.client.get("/grafana/api/health", cookies=again)).status_code == 401


def sent_params(request: httpx.Request) -> list[tuple[str, str]]:
    body = request.content.decode() if request.content else ""
    return parse_qsl(request.url.query.decode()) + parse_qsl(body)


def ds(env: Env, tenant: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {env.service.ds_token(tenant)}"}


async def test_data_source_queries_are_scoped_to_the_tokens_tenant(env: Env) -> None:
    r = await env.client.get(
        "/internal/ds/prometheus/api/v1/query_range",
        params={"query": "sum(rate(haproxy_frontend_http_requests_total[1m]))", "step": "15"},
        headers=ds(env, TENANT),
    )
    assert r.status_code == 200
    seen = env.upstream.requests[-1]
    assert seen.url.host == "prometheus.internal" and seen.url.path == "/api/v1/query_range"
    assert dict(sent_params(seen))["query"] == (
        f'sum(rate(haproxy_frontend_http_requests_total{{tenant="{TENANT}"}}[1m]))'
    )
    await env.client.get(
        "/internal/ds/prometheus/api/v1/query", params={"query": "up"}, headers=ds(env, env.other)
    )
    assert dict(sent_params(env.upstream.requests[-1]))["query"] == f'up{{tenant="{env.other}"}}'


async def test_a_query_cannot_widen_the_scope(env: Env) -> None:
    await env.client.get(
        "/internal/ds/prometheus/api/v1/query",
        params={"query": f'up{{tenant="{env.other}"}} or {{tenant=~".+"}}'},
        headers=ds(env, TENANT),
    )
    query = dict(sent_params(env.upstream.requests[-1]))["query"]
    # every selector keeps our matcher first; the caller's own matchers are ANDed after it
    assert query.count(f'{{tenant="{TENANT}",') == 2
    assert query.index(f'tenant="{TENANT}"') < query.index(f'tenant="{env.other}"')


async def test_parameters_cannot_be_smuggled_around_the_rewrite(env: Env) -> None:
    r = await env.client.post(
        "/internal/ds/prometheus/api/v1/query?query=secret_metric",
        content="query=up",
        headers={**ds(env, TENANT), "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 200
    seen = env.upstream.requests[-1]
    assert not seen.url.query  # everything travels in one place: the body
    assert [v for k, v in sent_params(seen) if k == "query"] == [
        f'secret_metric{{tenant="{TENANT}"}}',
        f'up{{tenant="{TENANT}"}}',
    ]


async def test_label_endpoints_are_confined_to_the_tenant(env: Env) -> None:
    await env.client.get(
        "/internal/ds/prometheus/api/v1/label/tenant/values", headers=ds(env, TENANT)
    )
    assert sent_params(env.upstream.requests[-1]) == [("match[]", f'{{tenant="{TENANT}"}}')]
    await env.client.get(
        "/internal/ds/loki/loki/api/v1/label/frontend/values", headers=ds(env, TENANT)
    )
    seen = env.upstream.requests[-1]
    assert (
        seen.url.host == "loki.internal" and seen.url.path == "/loki/api/v1/label/frontend/values"
    )
    assert sent_params(seen) == [("query", f'{{tenant="{TENANT}"}}')]
    await env.client.get(
        "/internal/ds/loki/loki/api/v1/query_range",
        params={"query": '{frontend="vs_web"} |= "x"'},
        headers=ds(env, TENANT),
    )
    assert dict(sent_params(env.upstream.requests[-1]))["query"] == (
        f'{{tenant="{TENANT}",frontend="vs_web"}} |= "x"'
    )


@pytest.mark.parametrize(
    "path",
    [
        "prometheus/api/v1/status/config",
        "prometheus/api/v1/targets",
        "prometheus/api/v1/rules",
        "prometheus/api/v1/admin/tsdb/delete_series",
        "prometheus/federate",
        "prometheus/api/v1/label/../../status/config/values",
        "prometheus/query",
        "loki/loki/api/v1/tail",
        "loki/loki/api/v1/push",
        "tempo/anything",
    ],
)
async def test_other_backend_endpoints_are_refused(env: Env, path: str) -> None:
    before = len(env.upstream.requests)
    r = await env.client.get(
        f"/internal/ds/{path}", params={"query": "up"}, headers=ds(env, TENANT)
    )
    assert r.status_code in {403, 404, 400}
    assert len(env.upstream.requests) == before


async def test_data_source_tokens_are_checked(env: Env) -> None:
    for headers in (
        {},
        {"Authorization": "Bearer nope"},
        {"Authorization": f"Bearer sygd_{TENANT.hex}.{'0' * 48}"},
        {"Authorization": f"Bearer {env.tokens['root']}"},
        {"Authorization": f"Bearer {env.service.ds_token(TENANT)[:-1]}0"},
    ):
        r = await env.client.get(
            "/internal/ds/prometheus/api/v1/query", params={"query": "up"}, headers=headers
        )
        assert r.status_code == 401, headers
    # a token made with another secret, and a tenant's token swapped onto another tenant's id
    foreign = GrafanaService(
        env.w.db, GrafanaConfig(INTERNAL, "a", "p", "x" * 40, "http://c", "http://p"), FakeGrafana()
    )
    r = await env.client.get(
        "/internal/ds/prometheus/api/v1/query",
        params={"query": "up"},
        headers=ds_for(foreign, TENANT),
    )
    assert r.status_code == 401
    swapped = env.service.ds_token(TENANT).replace(TENANT.hex, env.other.hex)
    r = await env.client.get(
        "/internal/ds/prometheus/api/v1/query",
        params={"query": "up"},
        headers={"Authorization": f"Bearer {swapped}"},
    )
    assert r.status_code == 401
    assert not env.upstream.requests


def ds_for(service: GrafanaService, tenant: uuid.UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {service.ds_token(tenant)}"}


async def test_unconfigured_controller_answers_404(pg_url: str) -> None:
    async with world(pg_url) as w:
        services = build_services(w.db, StaticEngineDirectory({}))
        token = (await services.tokens.create(ROOT, TENANT, "v", Role.VIEWER))[0]
        app = create_app(services)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as client:
            r = await client.post(
                f"{V1}/tenants/acme/grafana/launch",
                json={},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 404 and r.json()["code"] == "not_enabled"
            assert (await client.get("/grafana/api/health")).status_code == 404
            assert (
                await client.get("/internal/ds/prometheus/api/v1/query?query=up")
            ).status_code == 404
            me = await client.get(f"{V1}/me", headers={"Authorization": f"Bearer {token}"})
            assert me.json()["links"]["grafana_embedded"] is False


async def test_admin_launch_uses_the_main_organization(env: Env) -> None:
    r = await env.client.post(f"{V1}/grafana/launch", json={}, headers=env.auth("root"))
    got = await env.client.get(r.json()["url"])
    assert got.status_code == 303
    login = grafana_login("token:r", None)
    assert env.grafana.members[1] == {login: "Viewer"}
    await env.client.get("/grafana/api/health", cookies={"sy_grafana": got.cookies["sy_grafana"]})
    assert env.upstream.requests[-1].headers["x-grafana-org-id"] == "1"
