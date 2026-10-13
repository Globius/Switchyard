# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Configuration backups: targets, a run, pruning, verify and failure records (ADR 0205)."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.adapters.backup_targets import BackupStore, MemoryStore
from switchyard.adapters.secrets import MemorySecrets
from switchyard.api.app import ApiServices, build_services, create_app
from switchyard.db.ops_models import AuditEventRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.backup import BackupTargetConfig, decrypt
from switchyard.domain.workflow import Role
from switchyard.pipeline.engines import StaticEngineDirectory
from switchyard.services.context import Principal
from tests.pipeline_env import GROUP, TENANT, World, engine_record, world

pytestmark = pytest.mark.usefixtures("owner_engine")

V1 = "/api/v1"
ROOT = Principal(actor="user:root", role=Role.SYSTEM_ADMIN)
PASSPHRASE = "correct horse battery staple"
TARGET = {"name": "nightly", "passphrase_ref": "r/pass", "target": {"kind": "local"}, "keep": 2}


class Rig:
    def __init__(
        self,
        w: World,
        services: ApiServices,
        client: httpx.AsyncClient,
        store: MemoryStore,
        secrets: MemorySecrets,
    ) -> None:
        self.w, self.services, self.client = w, services, client
        self.store, self.secrets = store, secrets

    async def token(self, role: Role) -> dict[str, str]:
        tenant = None if role is Role.SYSTEM_ADMIN else TENANT
        plaintext, _ = await self.services.tokens.create(
            ROOT, tenant, f"t{uuid.uuid4().hex[:8]}", role
        )
        return {"Authorization": f"Bearer {plaintext}"}


@pytest_asyncio.fixture
async def rig(pg_url: str, tmp_path: Path) -> AsyncIterator[Rig]:
    async with world(pg_url) as w:
        directory = StaticEngineDirectory(
            {GROUP: [(engine_record(n), e) for n, e in w.engines.items()]}
        )
        secrets = MemorySecrets({"r/pass": PASSPHRASE})
        services = build_services(w.db, directory, secrets)
        store = MemoryStore()

        async def factory(_: BackupTargetConfig) -> BackupStore:
            return store

        services.backup._factory = factory
        transport = httpx.ASGITransport(app=create_app(services))
        async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
            yield Rig(w, services, client, store, secrets)


async def test_a_backup_is_encrypted_pruned_recorded_and_verifiable(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    assert (
        await rig.client.post(f"{V1}/system/backup-targets", json=TARGET, headers=root)
    ).status_code == 201
    names = []
    for reason in ("manual", "pre-upgrade", "manual"):
        res = await rig.client.post(
            f"{V1}/system/backups", json={"target": "nightly", "reason": reason}, headers=root
        )
        assert res.status_code == 201, res.text
        names.append(res.json()["archive"])
        await asyncio.sleep(1.1)  # archive names carry a one-second stamp
    first = res.json()
    assert first["status"] == "ok" and first["size_bytes"] > 0 and first["tenants"] >= 1

    # The oldest archives beyond keep=2 are gone, the content is encrypted and opens with the passphrase.
    assert len(rig.store.files) == 2 and names[0] not in rig.store.files
    blob = rig.store.files[sorted(rig.store.files)[-1]]
    assert b"virtual_services" not in blob
    doc = json.loads(decrypt(blob, PASSPHRASE))
    assert doc["format"] == 1 and doc["tenants"]

    verified = await rig.client.post(
        f"{V1}/system/backups/verify",
        json={"target": "nightly", "archive": sorted(rig.store.files)[-1]},
        headers=root,
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["controller_version"] == doc["controller_version"]

    runs = (await rig.client.get(f"{V1}/system/backups", headers=root)).json()
    assert [r["status"] for r in runs] == ["ok"] * 3
    async with admin_session(rig.w.db.admin) as session:
        actions = set((await session.execute(select(AuditEventRow.action))).scalars())
    assert {"backup_target.created", "backup.created"} <= actions


async def test_a_failed_backup_is_recorded_without_credentials(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    bad = {**TARGET, "name": "broken", "passphrase_ref": "r/missing"}
    await rig.client.post(f"{V1}/system/backup-targets", json=bad, headers=root)
    res = await rig.client.post(f"{V1}/system/backups", json={"target": "broken"}, headers=root)
    assert res.status_code == 409
    runs = (await rig.client.get(f"{V1}/system/backups", headers=root)).json()
    assert runs[0]["status"] == "failed" and "r/missing" in runs[0]["error"]
    assert PASSPHRASE not in json.dumps(runs)


async def test_target_settings_are_validated_and_unique(rig: Rig) -> None:
    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.client.post(f"{V1}/system/backup-targets", json=TARGET, headers=root)
    again = await rig.client.post(f"{V1}/system/backup-targets", json=TARGET, headers=root)
    assert again.status_code == 409
    bad = {**TARGET, "name": "x", "target": {"kind": "s3", "endpoint": "http://plain"}}
    assert (
        await rig.client.post(f"{V1}/system/backup-targets", json=bad, headers=root)
    ).status_code == 422
    gone = await rig.client.delete(f"{V1}/system/backup-targets/nightly", headers=root)
    assert gone.status_code == 204
    missing = await rig.client.post(
        f"{V1}/system/backups", json={"target": "nightly"}, headers=root
    )
    assert missing.status_code == 404


async def test_only_system_administrators_may_back_up(rig: Rig) -> None:
    admin = await rig.token(Role.TENANT_ADMIN)
    res = await rig.client.post(f"{V1}/system/backups", json={"target": "nightly"}, headers=admin)
    assert res.status_code == 403


async def test_restore_refuses_a_live_controller_and_loads_a_fresh_one(
    rig: Rig, owner_engine: AsyncEngine
) -> None:
    from sqlalchemy import text

    root = await rig.token(Role.SYSTEM_ADMIN)
    await rig.client.post(f"{V1}/system/backup-targets", json=TARGET, headers=root)
    made = (
        await rig.client.post(f"{V1}/system/backups", json={"target": "nightly"}, headers=root)
    ).json()
    body = {"target": "nightly", "archive": made["archive"]}

    live = await rig.client.post(f"{V1}/system/backups/restore", json=body, headers=root)
    assert live.status_code == 409 and "already exist" in live.json()["detail"]

    # Empty the controller of tenants (the cascade removes their objects), as a fresh install is.
    async with owner_engine.begin() as conn:
        await conn.execute(text("TRUNCATE tenant CASCADE"))
    root = await rig.token(Role.SYSTEM_ADMIN)  # the cascade took the old tokens too
    dry = await rig.client.post(
        f"{V1}/system/backups/restore", json={**body, "dry_run": True}, headers=root
    )
    assert dry.status_code == 200 and dry.json()["restored"] is False
    done = await rig.client.post(f"{V1}/system/backups/restore", json=body, headers=root)
    assert done.status_code == 200, done.text
    assert done.json()["restored"] is True and done.json()["tenants"]
    # A second backup now contains what the first one did.
    again = await rig.client.post(f"{V1}/system/backups/verify", json=body, headers=root)
    assert again.json()["tenants"] == done.json()["tenants"]
