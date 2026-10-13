# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The release feed: settings, a signed fetch, refusal of bad feeds, and the update advice (ADR 0205)."""

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.domain.releases import public_key_b64, sign_feed
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from switchyard.services.release_feed import ReleaseFeedService
from tests.pipeline_env import GROUP, TENANT, World, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

V1 = "/api/v1"
ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
URL = "https://releases.example.test/releases.json"
KEY = Ed25519PrivateKey.generate()
PAYLOAD = {
    "schema": 1,
    "releases": [
        {
            "version": "9.0.0",
            "date": "2026-12-01",
            "notes": {"breaking": ["Removed X"], "action_required": ["Back up first"]},
            "upgrade_from": ["0.1.0"],
        }
    ],
}


class Rig:
    def __init__(self, w: World, services: ApiServices, client: httpx.AsyncClient) -> None:
        self.w, self.services, self.client = w, services, client
        self.body: bytes = b""
        self.status = 200
        self.requests: list[httpx.Request] = []

    async def token(self, role: Role) -> dict[str, str]:
        tenant = None if role is Role.SYSTEM_ADMIN else TENANT
        plaintext, _ = await self.services.tokens.create(
            ROOT, tenant, f"t{uuid.uuid4().hex[:8]}", role
        )
        return {"Authorization": f"Bearer {plaintext}"}

    def serve(self, doc: Any) -> None:
        self.body = json.dumps(doc).encode()


@pytest_asyncio.fixture
async def rig(pg_url: str) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        services = build_services(w.db, directory)
        holder: dict[str, Rig] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            holder["rig"].requests.append(request)
            return httpx.Response(holder["rig"].status, content=holder["rig"].body)

        services.release_feed._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            holder["rig"] = Rig(w, services, client)
            yield holder["rig"]


def settings(**over: Any) -> dict[str, Any]:
    return {
        "url": URL,
        "enabled": True,
        "trusted_keys": {"k1": public_key_b64(KEY)},
        **over,
    }


async def test_a_signed_feed_produces_an_update_recommendation(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    put = await rig.client.put(f"{V1}/system/releases", json=settings(), headers=root)
    assert put.status_code == 200, put.text
    rig.serve(sign_feed(PAYLOAD, KEY, "k1"))
    checked = await rig.client.post(f"{V1}/system/releases/check", headers=root)
    assert checked.status_code == 200 and checked.json()["ok"] is True
    assert checked.json()["latest_version"] == "9.0.0"
    request = rig.requests[0]
    assert str(request.url) == URL and request.content == b"" and "cookie" not in request.headers

    check = (await rig.client.get(f"{V1}/system/upgrade/check", headers=root)).json()
    codes = [f["code"] for f in check["findings"]]
    assert "update_available" in codes
    assert check["latest_version"] == "9.0.0"
    assert check["update"]["notes"]["breaking"] == ["Removed X"]
    assert check["update"]["path"] == ["9.0.0"]


async def test_a_feed_with_a_bad_signature_is_ignored(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.client.put(f"{V1}/system/releases", json=settings(), headers=root)
    rig.serve(sign_feed(PAYLOAD, Ed25519PrivateKey.generate(), "k1"))
    res = (await rig.client.post(f"{V1}/system/releases/check", headers=root)).json()
    assert res["ok"] is False and "does not verify" in res["error"]
    assert res["releases"] == [] and res["latest_version"] is None
    check = (await rig.client.get(f"{V1}/system/upgrade/check", headers=root)).json()
    assert "update_available" not in [f["code"] for f in check["findings"]]


async def test_a_failed_fetch_keeps_the_last_good_feed(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.client.put(f"{V1}/system/releases", json=settings(), headers=root)
    rig.serve(sign_feed(PAYLOAD, KEY, "k1"))
    await rig.client.post(f"{V1}/system/releases/check", headers=root)
    rig.status = 503
    res = (await rig.client.post(f"{V1}/system/releases/check", headers=root)).json()
    assert "HTTP 503" in res["error"] and res["latest_version"] == "9.0.0"


async def test_the_schedule_does_nothing_while_disabled(rig: Rig) -> None:
    svc: ReleaseFeedService = rig.services.release_feed
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.client.put(f"{V1}/system/releases", json=settings(enabled=False), headers=root)
    state = await svc.refresh()  # the job calls this without a principal
    assert rig.requests == [] and state.checked_at is None


async def test_settings_are_validated(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    for bad in (
        {"url": "http://plain.example.test/r.json"},
        {"url": "https://user:pw@example.test/r.json"},
        {"url": "", "enabled": True},
        {"url": URL, "trusted_keys": {"k": "short"}},
    ):
        res = await rig.client.put(f"{V1}/system/releases", json=bad, headers=root)
        assert res.status_code == 422, bad


async def test_only_system_administrators_may_change_it(rig: Rig) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    res = await rig.client.put(f"{V1}/system/releases", json=settings(), headers=admin)
    assert res.status_code == 403
