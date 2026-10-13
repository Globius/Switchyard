# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Upgrade bundles kept on the controller: upload or fetch, verify, list, plan, download (ADR 0205).

The controller never applies a bundle itself. It only checks one (signature and every file
hash), keeps the verified copy in a staging directory, and shows what applying it involves so an
administrator can fetch it for `syctl upgrade apply` on each node. A file is stored only after it
verifies, under a name made from its version and hash, so a name from a request is never a path.
System administrators only (permission ``manage controller``).
"""

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from switchyard import __version__
from switchyard.db import audit_log
from switchyard.db.tenancy import admin_session
from switchyard.domain import bundle as domain
from switchyard.domain.bundle import BundleError, Manifest
from switchyard.domain.versions import UNKNOWN, version_key
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import ConflictError, NotFoundError
from switchyard.services.release_bundle import verify
from switchyard.services.release_feed import PROJECT_KEYS, ReleaseFeedService

MAX_BYTES = int(os.environ.get("SWITCHYARD_MAX_BUNDLE_BYTES", str(4 * 1024**3)))
_NAME = re.compile(r"^switchyard-\d+\.\d+\.\d+(-[0-9A-Za-z.]{1,32})?-[0-9a-f]{12}\.sybundle$")
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class StagedBundle:
    """A verified bundle on the controller."""

    name: str
    version: str
    size_bytes: int
    sha256: str
    schema_head: str
    key_id: str
    notes: dict[str, list[str]]
    files: list[dict[str, Any]]


@dataclass(frozen=True)
class BundlePlan:
    """What applying a staged bundle involves for this controller."""

    bundle: StagedBundle
    running_version: str
    steps: list[str]
    warnings: list[str]


def _staged(name: str, manifest: Manifest, digest: str, size: int, key_id: str) -> StagedBundle:
    return StagedBundle(
        name=name,
        version=manifest.version,
        size_bytes=size,
        sha256=digest,
        schema_head=manifest.schema_head,
        key_id=key_id,
        notes=manifest.notes.model_dump(),
        files=[f.model_dump() for f in manifest.files],
    )


class BundleStaging:
    """Stage, list, plan and delete bundles under one directory."""

    def __init__(
        self,
        db: Databases,
        feed: ReleaseFeedService,
        root: Path,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """`root` is the staging directory; `client` is replaced in tests."""
        self._db, self._feed, self._root, self._client = db, feed, root, client

    async def _trusted(self) -> dict[str, str]:
        return {**PROJECT_KEYS, **(await self._feed.config()).trusted_keys}

    def _path(self, name: str) -> Path:
        if not _NAME.match(name):
            raise NotFoundError("bundle not found")
        return self._root / name

    async def _intake(
        self, principal: Principal, chunks: AsyncIterator[bytes], request_id: str | None, how: str
    ) -> StagedBundle:
        """Write the stream to a temporary file, verify it, then move it into place."""
        self._root.mkdir(parents=True, exist_ok=True)
        digest, size = hashlib.sha256(), 0
        fd, tmp_name = tempfile.mkstemp(dir=self._root, suffix=".part")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise BundleError(f"the bundle is larger than {MAX_BYTES} bytes")
                    digest.update(chunk)
                    await asyncio.to_thread(out.write, chunk)
            trusted = await self._trusted()
            manifest = await asyncio.to_thread(verify, tmp, trusted)
            key_id = json.loads(_read_member(tmp, domain.SIGNATURE))["key_id"]
            name = f"switchyard-{manifest.version}-{digest.hexdigest()[:12]}.sybundle"
            tmp.replace(self._root / name)
        except BundleError as exc:
            tmp.unlink(missing_ok=True)
            raise ConflictError(f"the bundle was refused: {exc}") from exc
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        staged = _staged(name, manifest, digest.hexdigest(), size, key_id)
        sidecar = self._root / f"{name}.json"
        sidecar.write_text(json.dumps(asdict(staged), sort_keys=True))
        async with admin_session(self._db.admin) as session:
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="bundle.staged",
                object_ref=f"bundle:{name}",
                diff={"version": manifest.version, "via": how, "size_bytes": size},
                request_id=request_id,
            )
        return staged

    async def upload(
        self, principal: Principal, chunks: AsyncIterator[bytes], *, request_id: str | None = None
    ) -> StagedBundle:
        """Verify and keep an uploaded bundle. Raises :class:`ConflictError` if it is refused."""
        require(principal, "manage", "controller")
        return await self._intake(principal, chunks, request_id, "upload")

    async def fetch(
        self, principal: Principal, version: str, *, request_id: str | None = None
    ) -> StagedBundle:
        """Download the bundle the release feed lists for ``version`` and keep it.

        The download must match the feed's SHA-256 and size, and then still verify as a bundle.
        """
        require(principal, "manage", "controller")
        feed = await self._feed.feed()
        release = next((r for r in feed.releases if r.version == version), None) if feed else None
        if release is None or release.bundle is None:
            raise NotFoundError(f"the release feed lists no bundle for {version}")
        ref = release.bundle
        client = self._client or httpx.AsyncClient(timeout=600.0, follow_redirects=False)

        async def stream() -> AsyncIterator[bytes]:
            digest = hashlib.sha256()
            try:
                async with client.stream("GET", ref.url) as res:
                    if res.status_code != 200:
                        raise BundleError(f"the download answered HTTP {res.status_code}")
                    async for chunk in res.aiter_bytes(_CHUNK):
                        digest.update(chunk)
                        yield chunk
            except httpx.HTTPError as exc:
                raise BundleError(f"the download failed ({type(exc).__name__})") from exc
            if digest.hexdigest() != ref.sha256:
                raise BundleError("the download does not match the SHA-256 in the feed")

        try:
            return await self._intake(principal, stream(), request_id, "feed")
        finally:
            if self._client is None:
                await client.aclose()

    async def list(self, principal: Principal) -> list[StagedBundle]:
        """Staged bundles, newest version first."""
        require(principal, "manage", "controller")
        if not self._root.is_dir():
            return []
        out = []
        for side in self._root.glob("*.sybundle.json"):
            try:
                data = json.loads(side.read_text())
                if (self._root / data["name"]).is_file():
                    out.append(StagedBundle(**data))
            except (ValueError, KeyError, TypeError):
                continue
        return sorted(
            out,
            key=lambda b: (version_key(b.version) or UNKNOWN, b.name),
            reverse=True,
        )

    async def get(self, principal: Principal, name: str) -> StagedBundle:
        """One staged bundle, or :class:`NotFoundError`."""
        require(principal, "manage", "controller")
        self._path(name)
        for b in await self.list(principal):
            if b.name == name:
                return b
        raise NotFoundError("bundle not found")

    async def plan(self, principal: Principal, name: str) -> BundlePlan:
        """What applying the staged bundle involves, against the version this controller runs."""
        staged = await self.get(principal, name)
        manifest = Manifest.model_validate(
            {
                "version": staged.version,
                "schema_head": staged.schema_head,
                "created_at": "",
                "upgrade_from": [],
                "files": staged.files,
                "notes": staged.notes,
            }
        )
        result = domain.plan(manifest, __version__)
        return BundlePlan(staged, __version__, result.steps, result.warnings)

    def file_path(self, name: str) -> Path:
        """The staged file, for download. Raises :class:`NotFoundError` for an unknown name."""
        path = self._path(name)
        if not path.is_file():
            raise NotFoundError("bundle not found")
        return path

    async def delete(
        self, principal: Principal, name: str, *, request_id: str | None = None
    ) -> None:
        """Remove a staged bundle."""
        require(principal, "manage", "controller")
        path = self._path(name)
        if not path.is_file():
            raise NotFoundError("bundle not found")
        path.unlink()
        (self._root / f"{name}.json").unlink(missing_ok=True)
        async with admin_session(self._db.admin) as session:
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="bundle.deleted",
                object_ref=f"bundle:{name}",
                request_id=request_id,
            )


def _read_member(path: Path, member: str) -> bytes:
    import tarfile

    with tarfile.open(path, "r") as tar:
        fh = tar.extractfile(member)
        return b"" if fh is None else fh.read()
