# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Configuration backups to a named destination (ADR 0205).

A backup is the desired state of every tenant (the same objects the Git mirror holds, with
``secret_ref`` values and never secret values), written as one encrypted archive. It is for moving
the configuration to another cluster and for a pre-upgrade safety copy. It is **not** a
database backup: users, tokens, identity-provider settings and the audit log come back only
from pgBackRest ([restore-database](../runbooks/restore-database.md)).

System administrators only (permission ``manage controller``).
"""

import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import delete, select, text

from switchyard import __version__
from switchyard.adapters.backup_targets import BackupStore, BackupTargetError, build_store
from switchyard.adapters.secrets import SecretNotFoundError, SecretsReader
from switchyard.db import audit_log
from switchyard.db.backup_models import BackupRunRow, BackupTargetRow
from switchyard.db.models import TenantRow
from switchyard.db.repository import add_snapshot, load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.backup import (
    BackupError,
    BackupTargetConfig,
    archive_name,
    decrypt,
    encrypt,
    prune,
)
from switchyard.domain.snapshot import Snapshot
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import AlreadyExistsError, ConflictError, FieldErrors, NotFoundError

FORMAT = 1
StoreFactory = Callable[[BackupTargetConfig], Awaitable[BackupStore]]


@dataclass(frozen=True)
class BackupRun:
    """The record of one backup attempt."""

    id: UUID
    target: str
    archive: str | None
    status: str
    error: str | None
    size_bytes: int
    sha256: str | None
    controller_version: str
    schema_revision: str | None
    tenants: int
    reason: str
    actor: str
    created_at: datetime


def _run_view(r: BackupRunRow) -> BackupRun:
    return BackupRun(
        r.id, r.target_name, r.archive_name, r.status, r.error, r.size_bytes, r.sha256,
        r.controller_version, r.schema_revision, r.tenants, r.reason, r.actor, r.created_at,
    )  # fmt: skip


def summarize(plain: bytes) -> dict[str, Any]:
    """What a decrypted archive contains (counts only, no object content).

    Raises:
        BackupError: The content is not a version-1 Switchyard archive.
    """
    try:
        doc = json.loads(plain)
        if doc["format"] != FORMAT:
            raise BackupError(f"unsupported archive format {doc['format']!r}")
        tenants = doc["tenants"]
        return {
            "controller_version": doc["controller_version"],
            "schema_revision": doc["schema_revision"],
            "created_at": doc["created_at"],
            "tenants": [
                {
                    "name": t["tenants"][0]["name"] if t["tenants"] else "",
                    "virtual_services": len(t["virtual_services"]),
                    "pools": len(t["pools"]),
                    "se_groups": len(t["se_groups"]),
                    "certificates": len(t["certificates"]),
                }
                for t in tenants
            ],
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise BackupError("the archive content is not a Switchyard backup") from exc


class BackupService:
    """Targets, running a backup, listing and verifying archives."""

    def __init__(
        self,
        db: Databases,
        secrets: SecretsReader | None,
        local_root: Path,
        store_factory: StoreFactory | None = None,
    ) -> None:
        """``store_factory`` is replaced in tests; the default opens real stores."""
        self._db, self._secrets, self._root = db, secrets, local_root
        self._factory = store_factory

    async def _store(self, config: BackupTargetConfig) -> BackupStore:
        if self._factory is not None:
            return await self._factory(config)
        if self._secrets is None:
            raise ConflictError("no secrets backend is configured")
        return await build_store(config, self._secrets, self._root)

    async def _passphrase(self, config: BackupTargetConfig) -> str:
        if self._secrets is None:
            raise ConflictError("no secrets backend is configured")
        try:
            return await self._secrets.get(config.passphrase_ref)
        except SecretNotFoundError as exc:
            raise ConflictError(f"no secret at {config.passphrase_ref}") from exc

    # -- targets ---------------------------------------------------------------------------

    async def create_target(
        self, principal: Principal, body: dict[str, Any], *, request_id: str | None = None
    ) -> BackupTargetConfig:
        """Add a destination. Raises :class:`FieldErrors` or :class:`AlreadyExistsError`."""
        require(principal, "manage", "controller")
        try:
            config = BackupTargetConfig.model_validate(body)
        except ValidationError as exc:
            errors = FieldErrors()
            for e in exc.errors():
                errors.add(".".join(str(p) for p in e["loc"]), "invalid", e["msg"])
            errors.raise_if_any()
            raise
        async with admin_session(self._db.admin) as session:
            if (
                await session.execute(
                    select(BackupTargetRow.id).where(BackupTargetRow.name == config.name)
                )
            ).first():
                raise AlreadyExistsError(f"backup target {config.name} already exists")
            session.add(
                BackupTargetRow(
                    id=uuid7(),
                    name=config.name,
                    config=config.model_dump(mode="json"),
                    created_by=principal.actor,
                )
            )
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="backup_target.created",
                object_ref=f"backup_target:{config.name}",
                diff={"kind": config.target.kind},
                request_id=request_id,
            )
        return config

    async def list_targets(self, principal: Principal) -> list[BackupTargetConfig]:
        """Every destination, by name."""
        require(principal, "manage", "controller")
        async with admin_session(self._db.admin) as session:
            rows = (
                await session.execute(select(BackupTargetRow).order_by(BackupTargetRow.name))
            ).scalars()
            return [BackupTargetConfig.model_validate(r.config) for r in rows]

    async def _target(self, name: str) -> BackupTargetConfig:
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(select(BackupTargetRow).where(BackupTargetRow.name == name))
            ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"backup target {name} not found")
        return BackupTargetConfig.model_validate(row.config)

    async def delete_target(
        self, principal: Principal, name: str, *, request_id: str | None = None
    ) -> None:
        """Forget a destination. Archives already written are left where they are."""
        require(principal, "manage", "controller")
        async with admin_session(self._db.admin) as session:
            res = await session.execute(delete(BackupTargetRow).where(BackupTargetRow.name == name))
            if res.rowcount == 0:  # type: ignore[attr-defined]
                raise NotFoundError(f"backup target {name} not found")
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="backup_target.deleted",
                object_ref=f"backup_target:{name}",
                request_id=request_id,
            )

    async def test_target(self, principal: Principal, name: str) -> list[str]:
        """Open the destination and list its archives, proving the credentials and path work."""
        require(principal, "manage", "controller")
        store = await self._store(await self._target(name))
        try:
            return await store.list()
        except BackupTargetError as exc:
            raise ConflictError(str(exc)) from exc

    # -- backups ---------------------------------------------------------------------------

    async def _collect(self) -> tuple[bytes, str | None, int]:
        async with admin_session(self._db.admin) as session:
            revision = (
                await session.execute(text("select version_num from alembic_version"))
            ).scalar()
            tenant_ids = list((await session.execute(select(TenantRow.id))).scalars())
        snapshots: list[dict[str, Any]] = []
        for tenant_id in sorted(tenant_ids):
            async with tenant_session(self._db.app, [tenant_id]) as session:
                snapshots.append((await load_snapshot(session)).model_dump(mode="json"))
        doc = {
            "format": FORMAT,
            "controller_version": __version__,
            "schema_revision": revision,
            "created_at": datetime.now(UTC).isoformat(),
            "tenants": snapshots,
        }
        return (
            json.dumps(doc, sort_keys=True, separators=(",", ":")).encode(),
            revision,
            len(snapshots),
        )

    async def run(
        self,
        principal: Principal,
        target: str,
        *,
        reason: str = "manual",
        request_id: str | None = None,
    ) -> BackupRun:
        """Write a backup to ``target`` and prune the oldest beyond its ``keep``.

        The run is recorded either way. A failure is recorded as ``failed`` with the reason (never
        credentials) and raised as :class:`ConflictError`.
        """
        require(principal, "manage", "controller")
        config = await self._target(target)
        now = datetime.now(UTC)
        name = archive_name(__version__, now)
        run = BackupRunRow(
            id=uuid7(), target_name=target, archive_name=name, status="ok",
            controller_version=__version__, reason=reason, actor=principal.actor,
        )  # fmt: skip
        failure: str | None = None
        try:
            plain, run.schema_revision, run.tenants = await self._collect()
            blob = encrypt(
                plain, await self._passphrase(config), salt=os.urandom(16), nonce=os.urandom(12)
            )
            run.size_bytes, run.sha256 = len(blob), hashlib.sha256(blob).hexdigest()
            store = await self._store(config)
            await store.put(name, blob)
            for old in prune([*await store.list()], config.keep):
                await store.delete(old)
        except (BackupTargetError, ConflictError) as exc:
            failure = str(exc)
            run.status, run.error = "failed", failure[:500]
        async with admin_session(self._db.admin) as session:
            session.add(run)
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="backup.created" if failure is None else "backup.failed",
                object_ref=f"backup_target:{target}",
                diff={"archive": name, "reason": reason, "error": run.error},
                request_id=request_id,
            )
            await session.flush()
            view = _run_view(run)
        if failure is not None:
            raise ConflictError(f"backup to {target} failed: {failure}")
        return view

    async def list_runs(self, principal: Principal, limit: int = 50) -> list[BackupRun]:
        """The latest backup attempts, newest first."""
        require(principal, "manage", "controller")
        async with admin_session(self._db.admin) as session:
            rows = (
                await session.execute(
                    select(BackupRunRow).order_by(BackupRunRow.created_at.desc()).limit(limit)
                )
            ).scalars()
            return [_run_view(r) for r in rows]

    async def last_ok(self) -> datetime | None:
        """When the latest successful backup finished (the upgrade check reports its age)."""
        async with admin_session(self._db.admin) as session:
            return (
                await session.execute(
                    select(BackupRunRow.created_at)
                    .where(BackupRunRow.status == "ok")
                    .order_by(BackupRunRow.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def verify(self, principal: Principal, target: str, archive: str) -> dict[str, Any]:
        """Download ``archive``, decrypt it and return what it contains. Changes nothing."""
        require(principal, "manage", "controller")
        config = await self._target(target)
        try:
            blob = await (await self._store(config)).get(archive)
            return summarize(decrypt(blob, await self._passphrase(config)))
        except (BackupTargetError, BackupError) as exc:
            raise ConflictError(str(exc)) from exc

    async def restore(
        self,
        principal: Principal,
        target: str,
        archive: str,
        *,
        dry_run: bool = False,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Load the tenants of ``archive`` into this controller, which must hold none of them.

        Restores desired state only (see the module note). A tenant whose id or name already
        exists is a :class:`ConflictError` and nothing is written: restore into a fresh controller,
        not over a live one. Restored tenants have no engines; redeploy or re-enroll afterwards.

        Args:
            principal: A system administrator.
            target: The backup target holding the archive.
            archive: The archive name.
            dry_run: Check and report what would be restored without writing.
            request_id: For the audit event.
        """
        require(principal, "manage", "controller")
        config = await self._target(target)
        try:
            blob = await (await self._store(config)).get(archive)
            plain = decrypt(blob, await self._passphrase(config))
            summary = summarize(plain)
        except (BackupTargetError, BackupError) as exc:
            raise ConflictError(str(exc)) from exc
        snapshots = [Snapshot.model_validate(t) for t in json.loads(plain)["tenants"]]
        async with admin_session(self._db.admin) as session:
            rows = (await session.execute(select(TenantRow.id, TenantRow.name))).all()
            ids, names = {i for i, _ in rows}, {n for _, n in rows}
            clashes = sorted(
                t.name for s in snapshots for t in s.tenants if t.id in ids or t.name in names
            )
            if clashes:
                raise ConflictError(f"tenants already exist here: {', '.join(clashes)}")
            if dry_run:
                return {**summary, "restored": False}
            for snapshot in snapshots:
                await add_snapshot(session, snapshot)
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="backup.restored",
                object_ref=f"backup_target:{target}",
                diff={"archive": archive, "tenants": len(snapshots)},
                request_id=request_id,
            )
        return {**summary, "restored": True}
