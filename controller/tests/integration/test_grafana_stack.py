"""Embedded Grafana against the real Prometheus, Loki and Grafana images (ADR 0080).

Opt-in because it pulls three images: ``SWITCHYARD_STACK_TESTS=1 pytest tests/integration/test_grafana_stack.py``.
The controller runs in this process on a real socket; Grafana (in Docker) sends its data source
queries back to it.

It proves what the unit tests can only assume: Prometheus and Loki accept the rewritten queries,
Grafana's auth proxy and organizations behave as the controller expects, and a person who reaches
*Grafana itself* (not the proxy) still sees only their tenant's data.
"""

import asyncio
import os
import socket
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
import uvicorn
from testcontainers.core.container import DockerContainer

from switchyard.adapters.grafana import HttpGrafana
from switchyard.adapters.telemetry import HttpTelemetry
from switchyard.api.app import build_services, create_app
from switchyard.db.models import TenantRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from switchyard.services.grafana import GrafanaConfig, GrafanaService, grafana_login
from tests.pipeline_env import TENANT, world

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("SWITCHYARD_STACK_TESTS") != "1",
        reason="set SWITCHYARD_STACK_TESTS=1 to run against real Prometheus, Loki and Grafana",
    ),
    pytest.mark.usefixtures("owner_engine"),
]

ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
LOKI_YAML = Path(__file__).parents[3] / "deploy" / "telemetry" / "loki" / "loki.yaml"
ADMIN_PASSWORD = "stack-test-admin"
TENANT_B = "0192f3a0-0000-7000-8000-00000000000b"
BIND_ALL = "0.0.0.0"  # noqa: S104 (Grafana, in Docker, calls the controller back)
HOST_GATEWAY = {"extra_hosts": {"host.docker.internal": "host-gateway"}}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return int(s.getsockname()[1])


def wait_http(url: str, ok: str = "", seconds: int = 120) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=3)
            if r.status_code == 200 and ok in r.text:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise AssertionError(f"{url} did not become ready")


class Stack:
    def __init__(self, prometheus: str, loki: str, grafana: str) -> None:
        self.prometheus, self.loki, self.grafana = prometheus, loki, grafana
        # One port for every test: Grafana keeps its data source URL for the whole run.
        self.controller_port = free_port()


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    tmp = tmp_path_factory.mktemp("stack")
    rules = tmp / "rules.yml"
    series = [
        ("haproxy_frontend_http_requests_total", TENANT, "vs_a", 10),
        ("haproxy_frontend_http_requests_total", TENANT_B, "vs_b", 1000),
        ("haproxy_frontend_current_sessions", TENANT, "vs_a", 3),
        ("haproxy_frontend_current_sessions", TENANT_B, "vs_b", 300),
    ]
    engines = {str(TENANT): "se-1", TENANT_B: "se-2"}
    engine_series = [  # what the node exporter reports for each engine (two vCPUs, 8 GiB)
        ("node_cpu_seconds_total", cpu, 100) for cpu in ("0", "1")
    ]
    rules.write_text(
        "groups:\n- name: t\n  interval: 1s\n  rules:\n"
        + "".join(
            f"  - record: {name}\n    expr: vector({value})\n"
            f'    labels: {{tenant: "{tenant}", proxy: {proxy}, engine: {engines[str(tenant)]}}}\n'
            for name, tenant, proxy, value in series
        )
        + "".join(
            f"  - record: {name}\n    expr: vector({value})\n"
            f'    labels: {{tenant: "{tenant}", engine: {engine}, cpu: "{cpu}", mode: idle}}\n'
            for tenant, engine in ((str(TENANT), "se-1"), (TENANT_B, "se-2"))
            for name, cpu, value in engine_series
        )
        + "".join(
            f"  - record: {name}\n    expr: vector({value})\n"
            f'    labels: {{tenant: "{tenant}", engine: {engine}}}\n'
            for tenant, engine in ((str(TENANT), "se-1"), (TENANT_B, "se-2"))
            for name, value in (
                ("node_memory_MemTotal_bytes", 8 * 2**30),
                ("node_memory_MemAvailable_bytes", 6 * 2**30),
            )
        )
    )
    config = tmp / "prometheus.yml"
    config.write_text(
        "global:\n  evaluation_interval: 1s\nrule_files: [/etc/prometheus/rules.yml]\n"
    )
    prom = (
        DockerContainer("prom/prometheus:v3.0.1")
        .with_exposed_ports(9090)
        .with_volume_mapping(str(config), "/etc/prometheus/prometheus.yml", "ro")
        .with_volume_mapping(str(rules), "/etc/prometheus/rules.yml", "ro")
    )
    loki = (
        DockerContainer("grafana/loki:3.3.2")
        .with_exposed_ports(3100)
        .with_command("-config.file=/etc/loki/loki.yaml")
        .with_volume_mapping(str(LOKI_YAML), "/etc/loki/loki.yaml", "ro")
    )
    grafana = (
        DockerContainer("grafana/grafana:11.4.0")
        .with_exposed_ports(3000)
        .with_kwargs(**HOST_GATEWAY)
        .with_env("GF_SECURITY_ADMIN_PASSWORD", ADMIN_PASSWORD)
        .with_env("GF_SERVER_ROOT_URL", "http://localhost/grafana/")
        .with_env("GF_AUTH_PROXY_ENABLED", "true")
        .with_env("GF_AUTH_PROXY_HEADER_NAME", "X-WEBAUTH-USER")
        .with_env("GF_AUTH_PROXY_HEADER_PROPERTY", "username")
        .with_env("GF_AUTH_PROXY_AUTO_SIGN_UP", "false")
        .with_env("GF_AUTH_PROXY_WHITELIST", "127.0.0.1,172.16.0.0/12,192.168.0.0/16,10.0.0.0/8")
        .with_env("GF_AUTH_DISABLE_LOGIN_FORM", "true")
        .with_env("GF_AUTH_ANONYMOUS_ENABLED", "false")
        .with_env("GF_USERS_AUTO_ASSIGN_ORG", "false")
        .with_env("GF_USERS_ALLOW_ORG_CREATE", "false")
        .with_env("GF_USERS_VIEWERS_CAN_EDIT", "false")
        .with_env("GF_LIVE_MAX_CONNECTIONS", "0")
    )
    with prom, loki, grafana:
        base = "http://127.0.0.1"
        prometheus = f"{base}:{prom.get_exposed_port(9090)}"
        loki_url = f"{base}:{loki.get_exposed_port(3100)}"
        grafana_url = f"{base}:{grafana.get_exposed_port(3000)}"
        wait_http(prometheus + "/-/ready")
        wait_http(loki_url + "/ready", "ready")
        wait_http(grafana_url + "/api/health", "ok")
        wait_http(
            prometheus + "/api/v1/query?query=haproxy_frontend_http_requests_total", '"value"'
        )
        now = f"{int(datetime.now(UTC).timestamp())}000000000"
        for tenant, frontend, line in (
            (TENANT, "vs_a", "tenant-a-line"),
            (TENANT_B, "vs_b", "tenant-b-line"),
        ):
            httpx.post(
                loki_url + "/loki/api/v1/push",
                json={
                    "streams": [
                        {
                            "stream": {"tenant": str(tenant), "frontend": frontend},
                            "values": [
                                [now, f"1.2.3.4:5 [x] {frontend} p/s 0/0/0/1/1 200 5 {line}"]
                            ],
                        }
                    ]
                },
            ).raise_for_status()
        time.sleep(3)
        yield Stack(prometheus, loki_url, grafana_url)


class Live:
    def __init__(self, api: str, stack: Stack, service: GrafanaService, tokens: dict[str, str]):
        self.api, self.stack, self.service, self.tokens = api, stack, service, tokens

    @asynccontextmanager
    async def open_grafana(self, who: str, tenant: str) -> AsyncIterator[httpx.AsyncClient]:
        """Launch through the API, redeem the ticket, yield a client holding the proxy cookie."""
        async with httpx.AsyncClient(base_url=self.api, timeout=60) as client:
            r = await client.post(
                f"/api/v1/tenants/{tenant}/grafana/launch",
                json={},
                headers={"Authorization": f"Bearer {self.tokens[who]}"},
            )
            assert r.status_code == 200, r.text
            redeemed = await client.get(r.json()["url"])
            assert redeemed.status_code == 303, redeemed.text
            yield client


@pytest_asyncio.fixture
async def live(stack: Stack, pg_url: str) -> AsyncIterator[Live]:
    async with world(pg_url) as w:
        port = stack.controller_port
        config = GrafanaConfig(
            internal_url=stack.grafana,
            admin_user="admin",
            admin_password=ADMIN_PASSWORD,
            ds_secret="stack-test-secret-" + "x" * 20,
            datasource_url=f"http://host.docker.internal:{port}/internal/ds",
            prometheus_url=stack.prometheus,
            loki_url=stack.loki,
        )
        grafana = GrafanaService(
            w.db, config, HttpGrafana(config.internal_url, "admin", ADMIN_PASSWORD)
        )
        services = build_services(w.db, StaticEngineDirectory({}), grafana=grafana)
        async with admin_session(w.db.admin) as session:
            session.add(TenantRow(id=UUID(TENANT_B), name="globex", quotas={}))
        tokens = {
            "acme": (await services.tokens.create(ROOT, TENANT, "a", Role.VIEWER))[0],
            "globex": (await services.tokens.create(ROOT, UUID(TENANT_B), "g", Role.VIEWER))[0],
        }
        server = uvicorn.Server(
            uvicorn.Config(create_app(services), host=BIND_ALL, port=port, log_level="warning"),
        )
        task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.1)
        try:
            yield Live(f"http://127.0.0.1:{port}", stack, grafana, tokens)
        finally:
            server.should_exit = True
            await task


def prom_query(expr: str, uid: str = "prometheus") -> dict[str, Any]:
    return {
        "queries": [
            {
                "refId": "A",
                "datasource": {"uid": uid},
                "expr": expr,
                "instant": True,
                "range": False,
                "format": "table",
            }
        ],
        "from": "now-5m",
        "to": "now",
    }


def numbers(response: httpx.Response) -> list[float]:
    """Every numeric cell of a Grafana query response."""
    assert response.status_code == 200, response.text
    out: list[float] = []
    for result in response.json()["results"].values():
        assert not result.get("error"), result
        for frame in result.get("frames", []):
            for column in frame["data"]["values"]:
                out += [v for v in column if isinstance(v, int | float)]
    return out


async def test_tenant_sees_only_their_own_metrics_through_the_proxy(live: Live) -> None:
    async with live.open_grafana("acme", "acme") as client:
        home = await client.get("/grafana/api/search?query=Virtual")
        assert home.status_code == 200 and "sy-tenant-services" in home.text
        sessions = numbers(
            await client.post(
                "/grafana/api/ds/query",
                json=prom_query("sum(haproxy_frontend_current_sessions)"),
            )
        )
        assert 3 in sessions and 300 not in sessions
        # asking for the other tenant explicitly, or for everything, changes nothing
        for expr in (
            f'sum(haproxy_frontend_current_sessions{{tenant="{TENANT_B}"}})',
            'sum({__name__=~"haproxy_.*"})',
            'sum(haproxy_frontend_current_sessions{tenant=~".+"} or haproxy_frontend_current_sessions)',
        ):
            got = numbers(await client.post("/grafana/api/ds/query", json=prom_query(expr)))
            assert 300 not in got and 1000 not in got and 303 not in got, expr


async def test_a_person_inside_grafana_still_sees_only_their_tenant(live: Live) -> None:
    """Skip the proxy: act as the tenant's Grafana user directly against Grafana."""
    async with live.open_grafana("acme", "acme"):
        pass
    login = grafana_login("token:a", TENANT)
    grafana = live.stack.grafana
    admin = httpx.AsyncClient(base_url=grafana, auth=("admin", ADMIN_PASSWORD))
    async with admin:
        org_a = (await admin.get(f"/api/orgs/name/sy-{TENANT}")).json()["id"]
    headers = {"X-WEBAUTH-USER": login, "X-Grafana-Org-Id": str(org_a)}
    async with httpx.AsyncClient(base_url=grafana, headers=headers) as direct:
        got = numbers(
            await direct.post(
                "/api/ds/query", json=prom_query("sum(haproxy_frontend_http_requests_total)")
            )
        )
        assert 10 in got and 1000 not in got
        # the organization list of this user is their own organization only
        orgs = (await direct.get("/api/user/orgs")).json()
        assert [o["orgId"] for o in orgs] == [org_a]
        # no other organization, and no administration
        for org in (1, org_a + 7):
            elsewhere = {**headers, "X-Grafana-Org-Id": str(org)}
            assert (await direct.get("/api/org", headers=elsewhere)).status_code in {401, 403}
            found = await direct.get("/api/search", headers=elsewhere)
            assert found.status_code in {401, 403} or found.json() == [], (org, found.text)
            query = prom_query("sum(haproxy_frontend_http_requests_total)")
            denied = await direct.post("/api/ds/query", json=query, headers=elsewhere)
            assert denied.status_code != 200 or not numbers(denied), org
        assert (await direct.get("/api/users")).status_code in {401, 403}
        made = await direct.post("/api/admin/users", json={"login": "x", "password": "y" * 20})
        assert made.status_code in {401, 403}
        assert (await direct.get("/api/orgs")).status_code in {401, 403}


async def test_logs_are_scoped_too(live: Live) -> None:
    async with live.open_grafana("acme", "acme") as client:
        body: dict[str, Any] = {
            "queries": [
                {
                    "refId": "A",
                    "datasource": {"uid": "loki"},
                    "expr": '{frontend=~".+"}',
                    "queryType": "range",
                }
            ],
            "from": "now-1h",
            "to": "now+1m",
        }
        r = await client.post("/grafana/api/ds/query", json=body)
        assert r.status_code == 200, r.text
        assert "tenant-a-line" in r.text and "tenant-b-line" not in r.text
        body["queries"] = [{**body["queries"][0], "expr": f'{{tenant="{TENANT_B}"}}'}]
        assert "tenant-b-line" not in (await client.post("/grafana/api/ds/query", json=body)).text


async def test_the_variable_query_of_the_dashboard_lists_only_own_services(live: Live) -> None:
    async with live.open_grafana("acme", "acme") as client:
        r = await client.get(
            "/grafana/api/datasources/uid/prometheus/resources/api/v1/label/proxy/values"
        )
        assert r.status_code == 200, r.text
        assert "vs_a" in r.text and "vs_b" not in r.text
        r = await client.get(
            "/grafana/api/datasources/uid/prometheus/resources/api/v1/label/tenant/values"
        )
        assert str(TENANT) in r.text and TENANT_B not in r.text and TENANT_B not in r.text


async def test_engine_metrics_run_against_real_prometheus(live: Live) -> None:
    """The engine queries are valid PromQL, scoped to the tenant and the engine."""
    telemetry = HttpTelemetry(live.stack.prometheus)
    got = await telemetry.engine_metrics(TENANT, "se-1", window_s=300, step_s=5)
    assert got.summary.vcpus == 2
    assert got.summary.memory_total_bytes == 8 * 2**30
    assert got.summary.memory_used_bytes == 2 * 2**30
    assert got.summary.haproxy_sessions == 3  # this tenant's engine, not the other's 300
    assert got.summary.cpu_percent is not None and got.haproxy_sessions
    # another tenant's engine name, or a name that does not exist, has no data (None, not 0)
    other = await telemetry.engine_metrics(TENANT, "se-2", window_s=300, step_s=5)
    assert other.summary.vcpus is None and other.summary.haproxy_sessions is None
    assert other.haproxy_sessions == [] and other.memory_used_bytes == []


async def test_the_data_source_proxy_is_not_a_door_to_other_tenants(live: Live) -> None:
    token = live.service.ds_token(TENANT)
    async with httpx.AsyncClient(base_url=live.api) as c:
        h = {"Authorization": f"Bearer {token}"}
        r = await c.get("/internal/ds/prometheus/api/v1/status/config", headers=h)
        assert r.status_code == 403
        r = await c.get("/internal/ds/prometheus/api/v1/targets", headers=h)
        assert r.status_code == 403
        r = await c.get(
            "/internal/ds/prometheus/api/v1/query",
            params={"query": "haproxy_frontend_current_sessions"},
            headers=h,
        )
        values = [v["value"][1] for v in r.json()["data"]["result"]]
        assert values == ["3"]
        assert (await c.get("/internal/ds/prometheus/api/v1/query?query=up")).status_code == 401
