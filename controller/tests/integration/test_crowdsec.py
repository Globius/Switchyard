# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The CrowdSec integration: settings API, key handling, roles, status, Sync now and the feed (ADR 0213)."""

import uuid
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from switchyard.adapters.crowdsec import FakeCrowdSec
from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.db.ops_models import JobRow
from switchyard.db.tenancy import admin_session
from switchyard.domain import models as dm
from switchyard.domain.loader import symbolic_uuid
from switchyard.domain.workflow import ChangeSetState, Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from switchyard.services.crowdsec import DEFAULT_KEY_REF
from switchyard.services.iplists import SYNC_IP_LISTS_JOB, IpListSync
from tests.pipeline_env import GROUP, TENANT, World, engine_record, world, world_data

pytestmark = pytest.mark.usefixtures("owner_engine")

V1 = "/api/v1"
ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
URL = "https://crowdsec.example.test:8080"
BAN = UUID(symbolic_uuid("ps-ban"))


class Rig:
    def __init__(
        self,
        w: World,
        services: ApiServices,
        client: httpx.AsyncClient,
        lapi: FakeCrowdSec,
        secrets: MemorySecrets,
    ) -> None:
        self.w, self.services, self.client, self.lapi, self.secrets = (
            w,
            services,
            client,
            lapi,
            secrets,
        )

    async def token(self, role: Role) -> dict[str, str]:
        tenant = None if role is Role.SYSTEM_ADMIN else TENANT
        plaintext, _ = await self.services.tokens.create(
            ROOT, tenant, f"t{uuid.uuid4().hex[:8]}", role
        )
        return {"Authorization": f"Bearer {plaintext}"}


def data() -> dict[str, Any]:
    d = world_data()
    d["policy_sets"] = [
        {
            "id": "ps-ban",
            "tenant_id": "t-acme",
            "name": "crowdsec-bans",
            "kind": "allow_list",
            "managed_by": "reputation",
            "feed": "crowdsec",
            "cidrs": [],
        }
    ]
    return d


@pytest_asyncio.fixture
async def rig(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Rig]:
    for name in ("URL", "API_KEY", "ORIGINS"):
        monkeypatch.delenv(f"SWITCHYARD_CROWDSEC_{name}", raising=False)
    async with world(pg_url, data()) as w:
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        secrets = MemorySecrets()
        lapi = FakeCrowdSec(banned=["192.0.2.0/24", "198.51.100.7/32"], key="s3cr3t-key")
        services = build_services(w.db, directory, secrets, crowdsec=None)
        services.crowdsec._client = lapi
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            yield Rig(w, services, client, lapi, secrets)


def settings(**over: Any) -> dict[str, Any]:
    return {"url": URL, "api_key": "s3cr3t-key", "origins": ["crowdsec", "CAPI"], **over}


async def test_the_key_goes_to_the_secrets_backend_and_never_comes_back(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    put = await rig.client.put(f"{V1}/system/crowdsec", json=settings(), headers=root)
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["url"] == URL and body["key_ref"] == DEFAULT_KEY_REF
    assert body["origins"] == ["crowdsec", "CAPI"] and body["source"] == "database"
    assert await rig.secrets.get(DEFAULT_KEY_REF) == "s3cr3t-key"
    assert "s3cr3t-key" not in put.text
    got = await rig.client.get(f"{V1}/system/crowdsec", headers=root)
    assert "s3cr3t-key" not in got.text and got.json()["configured"] is True
    async with admin_session(rig.w.db.admin) as session:  # nor in the database
        dump = (await session.execute(text("select value::text from system_setting"))).scalars()
        assert not any("s3cr3t-key" in v for v in dump)


async def test_test_connection_reports_success_and_failure(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.client.put(f"{V1}/system/crowdsec", json=settings(), headers=root)
    ok = (await rig.client.post(f"{V1}/system/crowdsec/test", headers=root)).json()
    assert ok == {"ok": True, "bans": 2, "error": None}
    assert rig.lapi.calls[-1] == (URL, "s3cr3t-key", frozenset({"crowdsec", "CAPI"}))
    await rig.secrets.put(DEFAULT_KEY_REF, "rotated-elsewhere")
    bad = (await rig.client.post(f"{V1}/system/crowdsec/test", headers=root)).json()
    assert bad["ok"] is False and "403" in bad["error"]


async def test_only_system_administrators_may_use_it(rig: Rig) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    for method, path in (
        ("GET", ""),
        ("PUT", ""),
        ("POST", "/test"),
        ("POST", "/sync"),
    ):
        res = await rig.client.request(
            method,
            f"{V1}/system/crowdsec{path}",
            json=settings() if method == "PUT" else None,
            headers=admin,
        )
        assert res.status_code == 403, (method, path)


async def test_settings_are_validated(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    for bad in (
        {"url": "ftp://x.example.test"},
        {"url": "https://u:p@x.example.test"},
        {"url": URL, "key_ref": "../etc/passwd"},
        {"url": URL, "origins": ["x"] * 21},
    ):
        res = await rig.client.put(f"{V1}/system/crowdsec", json=bad, headers=root)
        assert res.status_code == 422, bad


async def test_the_environment_is_the_fallback(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    monkeypatch.setenv("SWITCHYARD_CROWDSEC_URL", "https://env.example.test")
    monkeypatch.setenv("SWITCHYARD_CROWDSEC_API_KEY", "s3cr3t-key")
    got = (await rig.client.get(f"{V1}/system/crowdsec", headers=root)).json()
    assert got["source"] == "environment" and got["url"] == "https://env.example.test"
    assert (await rig.client.post(f"{V1}/system/crowdsec/test", headers=root)).json()["ok"]
    await rig.client.put(f"{V1}/system/crowdsec", json=settings(), headers=root)
    assert (await rig.client.get(f"{V1}/system/crowdsec", headers=root)).json()["url"] == URL


async def test_sync_now_queues_the_job_and_is_audited(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    res = await rig.client.post(f"{V1}/system/crowdsec/sync", headers=root)
    assert res.status_code == 202, res.text
    async with admin_session(rig.w.db.admin) as session:
        kinds = (await session.execute(select(JobRow.kind))).scalars().all()
    assert SYNC_IP_LISTS_JOB in kinds


async def test_the_feed_fills_a_reputation_list_and_status_is_recorded(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.client.put(f"{V1}/system/crowdsec", json=settings(), headers=root)
    sync = IpListSync(rig.w.db, None, rig.w.service, rig.services.crowdsec)
    assert await sync.run_tenant(TENANT) == 1
    for record in await rig.w.service.list_change_sets(
        ROOT_TENANT, TENANT, states={ChangeSetState.APPLYING}
    ):
        await rig.w.pipeline.run(TENANT, record.id)
    policy = next(p for p in (await rig.w.snapshot()).policy_sets if p.id == BAN)
    assert isinstance(policy, dm.AllowListPolicySet)
    assert [str(c) for c in policy.cidrs] == ["192.0.2.0/24", "198.51.100.7/32"]
    state = (await rig.client.get(f"{V1}/system/crowdsec", headers=root)).json()
    assert state["ok"] is True and state["bans"] == 2 and state["synced_at"]


async def test_a_failed_read_keeps_the_list_and_shows_the_error(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.client.put(f"{V1}/system/crowdsec", json=settings(), headers=root)
    sync = IpListSync(rig.w.db, None, rig.w.service, rig.services.crowdsec)
    await sync.run_tenant(TENANT)
    for record in await rig.w.service.list_change_sets(
        ROOT_TENANT, TENANT, states={ChangeSetState.APPLYING}
    ):
        await rig.w.pipeline.run(TENANT, record.id)
    rig.lapi.key = "something-else"
    assert (
        await IpListSync(rig.w.db, None, rig.w.service, rig.services.crowdsec).run_tenant(TENANT)
        == 0
    )
    state = (await rig.client.get(f"{V1}/system/crowdsec", headers=root)).json()
    assert state["ok"] is False and "403" in state["error"] and state["bans"] == 2


ROOT_TENANT = Principal("system:iplists", Role.OPERATOR, frozenset({TENANT}))
