# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Staging upgrade bundles on the controller: upload, fetch, plan, download, delete (ADR 0205)."""

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.domain.releases import public_key_b64, sign_feed
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.bundle_staging import BundleStaging
from switchyard.services.context import Principal
from switchyard.services.release_bundle import Input, build
from tests.pipeline_env import GROUP, TENANT, World, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

V1 = "/api/v1"
ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
KEY = Ed25519PrivateKey.generate()
FEED_URL = "https://releases.example.test/releases.json"
BUNDLE_URL = "https://releases.example.test/switchyard-0.2.0.sybundle"


def make_bundle(tmp: Path, key: Ed25519PrivateKey = KEY, version: str = "0.2.0") -> bytes:
    src = tmp / "compose.yaml"
    src.write_text("services: {}\n")
    out = tmp / f"b-{uuid.uuid4().hex[:6]}.sybundle"
    build(
        out,
        version=version,
        schema_head="0047",
        created_at="2026-10-13T04:00:00Z",
        inputs=[Input("compose", src, "compose.yaml")],
        key=key,
        key_id="k1",
        upgrade_from=["0.1.0"],
    )
    return out.read_bytes()


class Rig:
    def __init__(
        self, w: World, services: ApiServices, client: httpx.AsyncClient, tmp: Path
    ) -> None:
        self.w, self.services, self.client, self.tmp = w, services, client, tmp
        self.downloads: dict[str, bytes] = {}

    async def token(self, role: Role) -> dict[str, str]:
        tenant = None if role is Role.SYSTEM_ADMIN else TENANT
        plaintext, _ = await self.services.tokens.create(
            ROOT, tenant, f"t{uuid.uuid4().hex[:8]}", role
        )
        return {"Authorization": f"Bearer {plaintext}"}

    async def trust(self, h: dict[str, str], feed: bool = False) -> None:
        body: dict[str, Any] = {
            "url": FEED_URL if feed else "",
            "enabled": feed,
            "trusted_keys": {"k1": public_key_b64(KEY)},
        }
        assert (
            await self.client.put(f"{V1}/system/releases", json=body, headers=h)
        ).status_code == 200


@pytest_asyncio.fixture
async def rig(pg_url: str, tmp_path: Path) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        services = build_services(w.db, directory)
        holder: dict[str, Rig] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            body = holder["rig"].downloads.get(str(request.url))
            return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        services.release_feed._client = http
        services.bundles = BundleStaging(w.db, services.release_feed, tmp_path / "stage", http)
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            holder["rig"] = Rig(w, services, client, tmp_path)
            yield holder["rig"]


async def test_upload_plan_download_and_delete(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.trust(root)
    blob = make_bundle(rig.tmp)
    up = await rig.client.put(f"{V1}/system/bundles", content=blob, headers=root)
    assert up.status_code == 201, up.text
    staged = up.json()
    assert staged["version"] == "0.2.0" and staged["key_id"] == "k1"
    assert staged["sha256"] == hashlib.sha256(blob).hexdigest() and staged["size_bytes"] == len(
        blob
    )
    name = staged["name"]

    listed = (await rig.client.get(f"{V1}/system/bundles", headers=root)).json()
    assert [b["name"] for b in listed] == [name]
    plan = (await rig.client.get(f"{V1}/system/bundles/{name}", headers=root)).json()
    assert plan["running_version"] and any("Back up" in s for s in plan["steps"])
    assert plan["bundle"]["version"] == "0.2.0"
    got = await rig.client.get(f"{V1}/system/bundles/{name}/file", headers=root)
    assert got.status_code == 200 and got.content == blob

    assert (await rig.client.delete(f"{V1}/system/bundles/{name}", headers=root)).status_code == 204
    assert (await rig.client.get(f"{V1}/system/bundles", headers=root)).json() == []
    assert (await rig.client.get(f"{V1}/system/bundles/{name}", headers=root)).status_code == 404


async def test_bundles_that_do_not_verify_are_refused_and_not_kept(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.trust(root)
    stranger = make_bundle(rig.tmp, Ed25519PrivateKey.generate())
    res = await rig.client.put(f"{V1}/system/bundles", content=stranger, headers=root)
    assert res.status_code == 409 and "refused" in res.json()["detail"]
    junk = await rig.client.put(f"{V1}/system/bundles", content=b"not a bundle" * 100, headers=root)
    assert junk.status_code == 409
    changed = bytearray(make_bundle(rig.tmp))
    changed[changed.index(b"services: {}") + 3] ^= 1
    res = await rig.client.put(f"{V1}/system/bundles", content=bytes(changed), headers=root)
    assert res.status_code == 409
    assert (await rig.client.get(f"{V1}/system/bundles", headers=root)).json() == []
    stage = rig.tmp / "stage"
    assert not stage.exists() or not list(stage.iterdir())  # nothing left behind, not even .part


async def test_names_from_requests_are_never_paths(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    for bad in ("..%2f..%2fetc%2fpasswd", "x.sybundle", "switchyard-0.2.0-zzzz.sybundle"):
        res = await rig.client.get(f"{V1}/system/bundles/{bad}/file", headers=root)
        assert res.status_code in (404, 422), bad
        res = await rig.client.delete(f"{V1}/system/bundles/{bad}", headers=root)
        assert res.status_code in (404, 422), bad


async def test_fetch_from_the_feed_checks_the_digest(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.trust(root, feed=True)
    blob = make_bundle(rig.tmp)
    release: dict[str, Any] = {
        "version": "0.2.0",
        "date": "2026-10-20",
        "bundle": {
            "url": BUNDLE_URL,
            "sha256": hashlib.sha256(blob).hexdigest(),
            "size_bytes": len(blob),
        },
    }
    rig.downloads[FEED_URL] = json.dumps(
        sign_feed({"schema": 1, "releases": [release]}, KEY, "k1")
    ).encode()
    rig.downloads[BUNDLE_URL] = blob
    assert (await rig.client.post(f"{V1}/system/releases/check", headers=root)).json()["ok"] is True

    ok = await rig.client.post(
        f"{V1}/system/bundles/fetch", json={"version": "0.2.0"}, headers=root
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["sha256"] == hashlib.sha256(blob).hexdigest()

    rig.downloads[BUNDLE_URL] = blob + b"tampered"
    bad = await rig.client.post(
        f"{V1}/system/bundles/fetch", json={"version": "0.2.0"}, headers=root
    )
    assert bad.status_code == 409 and "SHA-256" in bad.json()["detail"]
    none = await rig.client.post(
        f"{V1}/system/bundles/fetch", json={"version": "9.9.9"}, headers=root
    )
    assert none.status_code == 404


async def test_only_system_administrators_may_stage(rig: Rig) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    res = await rig.client.put(f"{V1}/system/bundles", content=b"x", headers=admin)
    assert res.status_code == 403
    assert (await rig.client.get(f"{V1}/system/bundles", headers=admin)).status_code == 403
