# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The upgrade check API: versions, schema state and advice, system administrators only (ADR 0205)."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard import __version__
from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.db.ops_models import EngineStatusRow
from switchyard.db.tenancy import admin_session
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

    async def token(self, role: Role) -> dict[str, str]:
        tenant = None if role is Role.SYSTEM_ADMIN else TENANT
        plaintext, _ = await self.services.tokens.create(
            ROOT, tenant, f"t{uuid.uuid4().hex[:8]}", role
        )
        return {"Authorization": f"Bearer {plaintext}"}

    async def report(self, name: str, version: str | None) -> None:
        async with admin_session(self.w.db.admin) as session:
            session.add(
                EngineStatusRow(
                    engine_id=uuid.uuid4(),
                    tenant_id=TENANT,
                    se_group_id=GROUP,
                    name=name,
                    reachable=True,
                    agent={"version": version} if version else None,
                )
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
            yield Rig(w, services, client)


async def test_an_old_agent_is_advised_not_denied(rig: Rig) -> None:
    await rig.report("se-old", "0.0.1")
    await rig.report("se-new", __version__)
    await rig.report("se-dev", "dev")
    root = await rig.token(Role.SYSTEM_ADMIN)
    res = await rig.client.get(f"{V1}/system/upgrade/check", headers=root)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["controller_version"] == __version__
    assert body["schema_state"] == "current"
    by_subject = {f["subject"]: f for f in body["findings"]}
    assert by_subject["se-old"]["code"] == "agent_behind"
    assert by_subject["se-dev"]["code"] == "agent_dev_build"
    assert by_subject["se-dev"]["severity"] == "info"
    assert "se-new" not in by_subject
    assert {e["name"] for e in body["engines"]} >= {"se-old", "se-new", "se-dev"}


async def test_a_database_from_a_newer_release_is_an_error(
    rig: Rig, owner_engine: AsyncEngine
) -> None:
    from sqlalchemy import text

    async with owner_engine.begin() as conn:
        await conn.execute(text("update alembic_version set version_num = '9999'"))
    root = await rig.token(Role.SYSTEM_ADMIN)
    body = (await rig.client.get(f"{V1}/system/upgrade/check", headers=root)).json()
    assert body["schema_state"] == "newer"
    codes = [f["code"] for f in body["findings"]]
    assert "schema_newer_than_code" in codes and "backup_missing" in codes


async def test_only_system_administrators_may_check(rig: Rig) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    res = await rig.client.get(f"{V1}/system/upgrade/check", headers=admin)
    assert res.status_code == 403
