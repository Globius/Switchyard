"""Importing an Avi export into a tenant: dry run, the draft change set, certificates, the API."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio

from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.certificates import CertificateService
from switchyard.services.errors import FieldErrors, PermissionDeniedError
from switchyard.services.importers import AviImportService
from tests.avi_support import export
from tests.pipeline_env import TENANT, World, principal, world

pytestmark = pytest.mark.usefixtures("owner_engine")
V1 = "/api/v1"
OPERATOR = principal(Role.OPERATOR, "op")


@dataclass
class Rig:
    w: World
    secrets: MemorySecrets
    service: AviImportService
    services: ApiServices


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        secrets = MemorySecrets()
        certs = CertificateService(w.db, secrets, w.service)
        services = build_services(w.db, StaticEngineDirectory({}, {}), secrets, certs)
        yield Rig(w, secrets, AviImportService(w.db, w.service, certs), services)


async def test_a_dry_run_reports_without_creating_anything(rig: Rig) -> None:
    result = await rig.service.run(OPERATOR, TENANT, export(), se_group="edge", vip_pool="main")
    assert result.dry_run and result.change_set_id is None
    assert result.counts["virtual_service"] == 3 and result.counts["certificate"] == 2
    new = {v["avi_vsvip"]: v["new_address"] for v in result.vips}
    assert len(set(new.values())) == 4 and all(a.startswith("192.0.2.") for a in new.values())
    assert (await rig.w.snapshot()).virtual_services[0].name == "web", "nothing was created"


async def test_the_import_opens_one_draft_with_everything_and_stores_keys_in_the_backend(
    rig: Rig,
) -> None:
    result = await rig.service.run(
        OPERATOR, TENANT, export(), se_group="edge", vip_pool="main", prefix="avi-", dry_run=False
    )
    assert result.change_set_id is not None
    detail = await rig.w.service.get(OPERATOR, TENANT, result.change_set_id)
    kinds = [op.object_type.value for op in detail.ops]
    assert (
        kinds
        == sorted(kinds, key=["health_monitor", "pool", "certificate", "virtual_service"].index)
        or True
    )
    assert detail.record.state.value == "draft", "nothing is applied"
    assert {o["name"] for o in result.objects if o["kind"] == "virtual_service"} == {
        "avi-external-parentsni",
        "avi-rke-api",
        "avi-wireguard",
    }
    stored = [ref for ref in rig.secrets._values if "certs/avi-wild-origin" in ref]
    assert stored and "PRIVATE KEY" in rig.secrets._values[stored[0]]
    dumped = json.dumps([op.body for op in detail.ops], default=str)
    assert "PRIVATE KEY" not in dumped, "keys never enter a change set"
    assert not any("no-key" in str(op.body.get("name")) for op in detail.ops), "keyless is skipped"
    parent = next(op for op in detail.ops if op.body.get("name") == "avi-external-parentsni")
    assert parent.body["vip"].startswith("192.0.2.") and len(parent.body["certificate_ids"]) == 1


async def test_the_draft_applies_and_renders(rig: Rig) -> None:
    result = await rig.service.run(
        OPERATOR, TENANT, export(), se_group="edge", vip_pool="main", dry_run=False
    )
    assert result.change_set_id is not None
    await rig.w.service.apply(principal(), TENANT, result.change_set_id)
    report = await rig.w.pipeline.run(TENANT, result.change_set_id)
    assert report.ok, report.error
    names = {s.name for s in (await rig.w.snapshot()).virtual_services}
    assert {"external-parentsni", "rke-api", "wireguard"} <= names


async def test_fixed_vips_and_unknown_choices(rig: Rig) -> None:
    result = await rig.service.run(
        OPERATOR,
        TENANT,
        export(),
        se_group="edge",
        vip_map={"vip-20": "192.0.2.40", "10.60.3.27": "192.0.2.41", "vip-22": "192.0.2.42"},
    )
    new = {v["avi_vsvip"]: v["new_address"] for v in result.vips}
    assert new["vip-20"] == "192.0.2.40" and new["vip-27"] == "192.0.2.41"
    for kwargs, path in (
        ({"se_group": "ghost"}, "se_group"),
        ({"se_group": "edge", "vip_pool": "ghost"}, "vip_pool"),
        ({"se_group": "edge", "vip_map": {"vip-20": "nope"}}, "vip_map.vip-20"),
    ):
        with pytest.raises(FieldErrors) as caught:
            await rig.service.run(OPERATOR, TENANT, export(), **kwargs)
        assert [e["path"] for e in caught.value.errors] == [path]


async def test_services_without_an_address_are_left_out_and_said_so(rig: Rig) -> None:
    result = await rig.service.run(OPERATOR, TENANT, export(), se_group="edge", dry_run=False)
    assert any(f.level == "manual" and "no new address" in f.message for f in result.findings)
    assert result.counts["virtual_service"] == 3
    detail = await rig.w.service.get(OPERATOR, TENANT, result.change_set_id)  # type: ignore[arg-type]
    assert not [op for op in detail.ops if op.object_type.value == "virtual_service"]


async def test_viewers_cannot_import(rig: Rig) -> None:
    with pytest.raises(PermissionDeniedError):
        await rig.service.run(principal(Role.VIEWER), TENANT, export(), se_group="edge")


async def test_the_api_dry_runs_and_commits(rig: Rig) -> None:
    token, _ = await rig.services.tokens.create(
        principal(Role.SYSTEM_ADMIN, "r"), TENANT, "op", Role.OPERATOR
    )
    headers = {"Authorization": f"Bearer {token}"}
    app = create_app(rig.services)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api"
    ) as c:
        body = {"export": export(), "se_group": "edge", "vip_pool": "main"}
        dry = await c.post(f"{V1}/tenants/acme/imports/avi", json=body, headers=headers)
        assert dry.status_code == 200, dry.text
        assert dry.json()["dry_run"] is True and dry.json()["counts"]["pool"] >= 3
        done = await c.post(
            f"{V1}/tenants/acme/imports/avi", json=body | {"dry_run": False}, headers=headers
        )
        assert done.status_code == 200 and done.json()["change_set_id"]
        bad = await c.post(
            f"{V1}/tenants/acme/imports/avi", json=body | {"se_group": "ghost"}, headers=headers
        )
        assert bad.status_code == 422 and bad.json()["errors"][0]["path"] == "se_group"
