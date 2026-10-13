# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Engines of a service, rendered config per engine, and the running config of an engine."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from switchyard.adapters.agent import FakeAgent
from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import build_services, create_app
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from tests.pipeline_env import CERT_SECRETS, GROUP, TENANT, VS, World, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
V1 = "/api/v1"


class Rig:
    def __init__(self, w: World, client: httpx.AsyncClient, token: dict[str, str]) -> None:
        self.w, self.client, self.token = w, client, token


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        records = [
            engine_record(n, agent=True, mgmt_ip=ip)
            for n, ip in (("se-a", "10.30.0.11"), ("se-b", "10.30.0.12"))
        ]
        agents = {r.id: FakeAgent(r.name) for r in records}
        async with admin_session(w.db.admin) as s:
            for r in records:
                await s.execute(
                    text(
                        "INSERT INTO service_engine (id, tenant_id, se_group_id, name, "
                        "dataplane_url, state, mgmt_ip, agent_url) VALUES (:i, :t, :g, :n, "
                        "'https://x', 'enabled', :ip, :a)"
                    ),
                    {
                        "i": r.id,
                        "t": TENANT,
                        "g": GROUP,
                        "n": r.name,
                        "ip": r.mgmt_ip,
                        "a": r.agent_url,
                    },
                )
        directory = StaticEngineDirectory(
            {GROUP: [(r, w.engines[r.name]) for r in records]}, agents
        )
        services = build_services(w.db, directory, MemorySecrets(CERT_SECRETS))
        plaintext, _ = await services.tokens.create(ROOT, TENANT, "viewer", Role.VIEWER)
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            rig = Rig(w, client, {"Authorization": f"Bearer {plaintext}"})
            rig.agents = agents  # type: ignore[attr-defined]
            rig.records = records  # type: ignore[attr-defined]
            yield rig


async def test_a_service_lists_the_engines_of_its_group(rig: Rig) -> None:
    r = await rig.client.get(f"{V1}/tenants/acme/virtual-services/{VS}/engines", headers=rig.token)
    assert r.status_code == 200
    assert [(e["name"], e["mgmt_ip"], e["state"]) for e in r.json()] == [
        ("se-a", "10.30.0.11", "enabled"),
        ("se-b", "10.30.0.12", "enabled"),
    ]
    missing = await rig.client.get(
        f"{V1}/tenants/acme/virtual-services/{uuid.uuid4()}/engines", headers=rig.token
    )
    assert missing.status_code == 404


async def test_rendered_config_for_an_engine_has_group_and_engine_files_without_secrets(
    rig: Rig,
) -> None:
    base = f"{V1}/tenants/acme/virtual-services/{VS}/engines"
    one = (await rig.client.get(f"{base}/{rig.records[0].id}/rendered", headers=rig.token)).json()  # type: ignore[attr-defined]
    two = (await rig.client.get(f"{base}/{rig.records[1].id}/rendered", headers=rig.token)).json()  # type: ignore[attr-defined]
    files = {f["file"]: f for f in one["files"]}
    assert files["haproxy/haproxy.cfg"]["scope"] == "group"
    assert files["haproxy/haproxy.cfg"]["language"] == "haproxy"
    assert one["fingerprint"] == two["fingerprint"] and len(one["fingerprint"]) == 64
    assert not any(
        f["file"].startswith("certs/") and f["file"].endswith(".pem") for f in one["files"]
    )
    assert "PRIVATE KEY" not in str(one)
    gone = await rig.client.get(f"{base}/{uuid.uuid4()}/rendered", headers=rig.token)
    assert gone.status_code == 404


async def test_running_config_compares_the_engine_with_the_desired_state(rig: Rig) -> None:
    url = f"{V1}/tenants/acme/engines/{rig.records[0].id}/running-config"  # type: ignore[attr-defined]
    ok = (await rig.client.get(url, headers=rig.token)).json()
    assert ok["reachable"] and ok["error"] is None
    assert ok["haproxy"]["matches_desired"] is True and "frontend" in ok["haproxy"]["content"]
    # Someone edits the engine by hand.
    engine = rig.w.engines["se-a"]
    engine.files["haproxy/haproxy.cfg"] = "# hotfix\n" + engine.files["haproxy/haproxy.cfg"]
    drifted = (await rig.client.get(url, headers=rig.token)).json()
    assert drifted["haproxy"]["matches_desired"] is False, "the stamp is intact, the text is not"
    assert drifted["haproxy"]["fingerprint_matches"] is True
    missing = await rig.client.get(
        f"{V1}/tenants/acme/engines/{uuid.uuid4()}/running-config", headers=rig.token
    )
    assert missing.status_code == 404


async def test_running_config_reports_agent_files_by_hash(rig: Rig) -> None:
    agent = rig.agents[rig.records[0].id]  # type: ignore[attr-defined]
    agent.files = {"keepalived/keepalived.conf": "stale"}
    agent.bundle_id = "old"
    url = f"{V1}/tenants/acme/engines/{rig.records[0].id}/running-config"  # type: ignore[attr-defined]
    out = (await rig.client.get(url, headers=rig.token)).json()
    assert out["agent"]["bundle_id"] == "old"
    by_path = {f["path"]: f for f in out["agent"]["files"]}
    assert by_path["keepalived/keepalived.conf"]["matches_desired"] is False
    assert "stale" not in str(out["agent"]), "only hashes, never contents"
