"""``/metrics`` and the Prometheus service-discovery list, over a real database."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from switchyard.api.app import build_services, create_app
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from switchyard.services.engines import EngineService
from switchyard.services.observability import Family, collect_metrics, render_exposition
from tests.pipeline_env import GROUP, TENANT, World, add_host_route, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
SCRAPE = "scrape-secret"


class Setup:
    def __init__(self, w: World, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
        self.w, self.client, self.admin = w, client, admin


@pytest_asyncio.fixture
async def setup(pg_url: str) -> AsyncIterator[Setup]:
    async with world(pg_url) as w:
        engines = EngineService(w.db)
        for name, ip in (("se-a", "192.0.2.10"), ("se-b", "2001:db8::11"), ("se-c", None)):
            await engines.register(
                ROOT,
                TENANT,
                se_group_id=GROUP,
                name=name,
                dataplane_url="https://198.51.100.1:5555",
                mgmt_ip=ip,
            )
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        services = build_services(w.db, directory)
        admin, _ = await services.tokens.create(ROOT, None, "root", Role.SYSTEM_ADMIN)
        app = create_app(services, metrics_token=SCRAPE, grafana_url="https://grafana.example.test")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as client:
            yield Setup(w, client, {"Authorization": f"Bearer {admin}"})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_discovery_lists_enabled_engines_with_tenant_group_and_services(
    setup: Setup,
) -> None:
    r = await setup.client.get("/api/v1/discovery/prometheus", headers=setup.admin)
    assert r.status_code == 200
    groups: list[dict[str, Any]] = r.json()
    assert [g["targets"] for g in groups] == [["192.0.2.10:8405"], ["[2001:db8::11]:8405"]]
    labels = groups[0]["labels"]
    assert labels["tenant"] == str(TENANT) and labels["tenant_name"] == "acme"
    assert labels["engine"] == "se-a" and labels["se_group"]
    assert labels["vs"].startswith(",") and labels["vs"].endswith(",") and ",web," in labels["vs"]
    node = await setup.client.get(
        "/api/v1/discovery/prometheus?exporter=node&port=9200", headers=setup.admin
    )
    assert node.json()[0]["targets"] == ["192.0.2.10:9200"]
    assert (
        await setup.client.get("/api/v1/discovery/prometheus?exporter=x", headers=setup.admin)
    ).status_code == 422


async def test_a_disabled_engine_is_not_a_target(setup: Setup) -> None:
    async with admin_session(setup.w.db.admin) as s:
        await s.execute(text("UPDATE service_engine SET state = 'disabled' WHERE name = 'se-a'"))
    r = await setup.client.get("/api/v1/discovery/prometheus", headers=setup.admin)
    assert [g["labels"]["engine"] for g in r.json()] == ["se-b"]


async def test_scrape_access_is_the_token_or_a_system_administrator(setup: Setup) -> None:
    c = setup.client
    for path in ("/metrics", "/api/v1/discovery/prometheus"):
        assert (await c.get(path)).status_code == 401
        assert (await c.get(path, headers=bearer("wrong"))).status_code == 401
        assert (await c.get(path, headers=bearer(SCRAPE))).status_code == 200
        assert (await c.get(path, headers=setup.admin)).status_code == 200
    tenant_admin, _ = await create_token(setup)
    assert (await c.get("/metrics", headers=bearer(tenant_admin))).status_code == 403
    # The scrape token opens nothing else.
    assert (await c.get("/api/v1/tenants", headers=bearer(SCRAPE))).status_code == 401


async def create_token(setup: Setup) -> tuple[str, Any]:
    from switchyard.services.tokens import TokenService

    return await TokenService(setup.w.db).create(
        ROOT, TENANT, f"t{uuid.uuid4().hex[:6]}", Role.TENANT_ADMIN
    )


async def test_metrics_report_jobs_change_sets_and_engines(setup: Setup) -> None:
    assert (await setup.w.run_change([add_host_route("one.example.test")])).ok
    families = {f.name: f for f in await collect_metrics(setup.w.db)}
    assert {
        "switchyard_info",
        "switchyard_jobs",
        "switchyard_job_duration_seconds",
        "switchyard_change_sets",
        "switchyard_engines",
        "switchyard_engines_drifted",
        "switchyard_drift_findings",
        "switchyard_status_age_seconds",
    } <= set(families)
    r = await setup.client.get("/metrics", headers=bearer(SCRAPE))
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    body = r.text
    assert "# TYPE switchyard_jobs gauge" in body
    assert 'switchyard_info{version="0.1.0"} 1' in body
    assert 'switchyard_engines{reachable="true"} 0' in body


def test_exposition_escapes_labels_and_formats_numbers() -> None:
    bs, nl = chr(92), chr(10)
    f = Family("x_total", "counter", "A counter.")
    f.add(1.5, a='q"' + bs + nl)
    f.add(2)
    out = render_exposition([f])
    escaped = "q" + bs + '"' + bs + bs + bs + "n"
    assert f'x_total{{a="{escaped}"}} 1.5' in out
    assert out.endswith("x_total 2" + nl)


async def test_me_shows_the_grafana_link_and_permissions_carry_their_group(setup: Setup) -> None:
    me = (await setup.client.get("/api/v1/me", headers=setup.admin)).json()
    assert me["links"] == {"grafana": "https://grafana.example.test", "grafana_embedded": False}
    matrix = (await setup.client.get("/api/v1/permissions", headers=setup.admin)).json()
    groups = {p["group"] for p in matrix["permissions"]}
    assert len(groups) == 8 and {"Read", "Change sets", "System"} <= groups


async def test_tenant_reads_count_the_people_with_a_grant(setup: Setup) -> None:
    from switchyard.db.auth_models import IdentityProviderRow, IdpRoleMappingRow, UserAccountRow

    async with admin_session(setup.w.db.admin) as s:
        provider = IdentityProviderRow(
            id=uuid.uuid4(),
            name="corp",
            protocol="oidc",
            issuer_url="https://idp.example.test",
            client_id="switchyard",
        )
        s.add(provider)
        await s.flush()
        s.add(
            IdpRoleMappingRow(
                id=uuid.uuid4(),
                provider_id=provider.id,
                group_name="acme-ops",
                scope_tenant_id=TENANT,
                role="operator",
            )
        )
        for name, groups in (("a", ["acme-ops"]), ("b", ["acme-ops", "x"]), ("c", ["other"])):
            s.add(
                UserAccountRow(
                    id=uuid.uuid4(),
                    provider_id=provider.id,
                    subject=f"sub-{name}",
                    username=name,
                    groups=groups,
                )
            )
    one = (await setup.client.get("/api/v1/tenants/acme", headers=setup.admin)).json()
    assert one["members"] == 2
    listed = (await setup.client.get("/api/v1/tenants", headers=setup.admin)).json()
    assert [t["members"] for t in listed if t["name"] == "acme"] == [2]


async def test_sdn_status_follows_the_apply_job(setup: Setup) -> None:
    async with admin_session(setup.w.db.admin) as s:
        await s.execute(text("UPDATE network SET managed = true, sdn_zone = 'z', vnet = 'v1'"))
    url = "/api/v1/tenants/acme/sdn/status"
    never = (await setup.client.get(url, headers=setup.admin)).json()
    assert {n["state"] for n in never} == {"never"} and len(never) >= 1
    job = await setup.client.post("/api/v1/tenants/acme/networks/apply-sdn", headers=setup.admin)
    assert job.status_code == 202
    queued = (await setup.client.get(url, headers=setup.admin)).json()
    assert {n["state"] for n in queued} == {"queued"}
    assert {n["job_id"] for n in queued} == {job.json()["job_id"]}
    names = [n["network"] for n in queued]
    async with admin_session(setup.w.db.admin) as s:
        await s.execute(
            text(
                "UPDATE job SET state = 'succeeded', started_at = now(), finished_at = now(), "
                "result = CAST(:r AS jsonb) WHERE kind = 'apply_sdn'"
            ),
            {"r": json.dumps({"networks": names[:1], "removed": []})},
        )
    done = {
        n["network"]: n["state"] for n in (await setup.client.get(url, headers=setup.admin)).json()
    }
    assert done[names[0]] == "succeeded"
    assert all(state == "never" for name, state in done.items() if name != names[0])
    async with admin_session(setup.w.db.admin) as s:
        await s.execute(text("UPDATE job SET state = 'failed', last_error = 'boom'"))
    failed = (await setup.client.get(url, headers=setup.admin)).json()
    assert {n["state"] for n in failed} == {"failed"} and failed[0]["error"] == "boom"
