# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build, verify and unpack upgrade bundles (ADR 0205).

A bundle is a plain tar archive: ``manifest.json``, ``manifest.sig`` and the files the manifest
lists under ``files/``. :func:`verify` checks the signature first and then every file's size and
SHA-256 while streaming, refuses links, devices and any member the manifest does not list, and
never extracts anything. :func:`unpack` verifies and then writes the files itself (no
``extractall``), so a hostile archive cannot write outside ``dest``.
"""

import hashlib
import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from switchyard.domain.bundle import (
    MANIFEST,
    SIGNATURE,
    BundleError,
    BundleFile,
    Kind,
    Manifest,
    check_member_path,
    manifest_bytes,
    parse_manifest,
    sign_manifest,
    verify_manifest,
)
from switchyard.domain.releases import ReleaseNotes

_CHUNK = 1024 * 1024
_MAX_MANIFEST = 1024 * 1024


@dataclass(frozen=True)
class Input:
    """A file to put in a bundle: its kind, where it is on disk, and its name inside."""

    kind: Kind
    source: Path
    name: str


def _sha256(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def build(
    out: Path,
    *,
    version: str,
    schema_head: str,
    created_at: str,
    inputs: list[Input],
    key: Ed25519PrivateKey | None,
    key_id: str = "",
    upgrade_from: list[str] | None = None,
    min_agent: str = "0.0.0",
    recommended_min_agent: str = "0.0.0",
    notes: ReleaseNotes | None = None,
) -> Manifest:
    """Write a bundle to ``out`` and return its manifest; signed unless ``key`` is ``None``.

    Raises:
        BundleError: A name is unsafe or used twice.
    """
    files = []
    for item in inputs:
        path = check_member_path(f"files/{item.kind}/{item.name}")
        digest, size = _sha256(item.source)
        files.append(BundleFile(path=path, kind=item.kind, sha256=digest, size_bytes=size))
    manifest = Manifest(
        version=version,
        schema_head=schema_head,
        created_at=created_at,
        upgrade_from=upgrade_from or [],
        min_agent=min_agent,
        recommended_min_agent=recommended_min_agent,
        notes=notes or ReleaseNotes(),
        files=files,
    )
    if len({f.path for f in files}) != len(files):
        raise BundleError("two files have the same name in the bundle")
    raw = manifest_bytes(manifest)
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as tar:
        members = [(MANIFEST, raw)]
        if key is not None:
            members.append((SIGNATURE, sign_manifest(raw, key, key_id)))
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tar.addfile(info, io.BytesIO(data))
        for item, entry in zip(inputs, files, strict=True):
            info = tarfile.TarInfo(entry.path)
            info.size, info.mode, info.mtime = entry.size_bytes, 0o644, 0
            with item.source.open("rb") as fh:
                tar.addfile(info, fh)
    return manifest


def _read_small(tar: tarfile.TarFile, name: str) -> bytes:
    try:
        member = tar.getmember(name)
    except KeyError as exc:
        raise BundleError(f"{name} is missing from the bundle") from exc
    if not member.isreg() or member.size > _MAX_MANIFEST:
        raise BundleError(f"{name} is not a regular file of a sensible size")
    fh = tar.extractfile(member)
    if fh is None:  # pragma: no cover - isreg() above
        raise BundleError(f"{name} cannot be read")
    return fh.read()


def verify(path: Path, trusted: dict[str, str], *, allow_unsigned: bool = False) -> Manifest:
    """Check the signature, the member list and every file's size and SHA-256.

    `allow_unsigned` skips the signature check only (the hashes are still checked); it exists for
    `syctl upgrade apply --allow-unsigned` and nothing else.

    Raises:
        BundleError: Anything wrong; nothing is extracted either way.
    """
    try:
        with tarfile.open(path, "r") as tar:
            raw = _read_small(tar, MANIFEST)
            manifest = (
                parse_manifest(raw)
                if allow_unsigned
                else verify_manifest(raw, _read_small(tar, SIGNATURE), trusted)
            )
            listed = {f.path: f for f in manifest.files}
            seen: set[str] = set()
            for member in tar.getmembers():
                if member.name in (MANIFEST, SIGNATURE):
                    continue
                entry = listed.get(member.name)
                if entry is None or not member.isreg() or member.name in seen:
                    raise BundleError(f"unexpected member in the bundle: {member.name[:80]!r}")
                seen.add(member.name)
                fh = tar.extractfile(member)
                if fh is None:  # pragma: no cover - isreg() above
                    raise BundleError(f"{member.name} cannot be read")
                digest, size = hashlib.sha256(), 0
                while chunk := fh.read(_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
                if size != entry.size_bytes or digest.hexdigest() != entry.sha256:
                    raise BundleError(f"{member.name} does not match the manifest")
            if seen != set(listed):
                raise BundleError("the bundle is missing files the manifest lists")
            return manifest
    except tarfile.TarError as exc:
        raise BundleError("the file is not a readable bundle") from exc


def unpack(
    path: Path, dest: Path, trusted: dict[str, str], *, allow_unsigned: bool = False
) -> Manifest:
    """Verify ``path`` and write its files under ``dest`` (created if needed).

    Returns the manifest. Files land at ``dest/<manifest path>``; ``dest`` must not already
    hold files of the same name, so an old unpack is never mixed with a new one.
    """
    manifest = verify(path, trusted, allow_unsigned=allow_unsigned)
    root = dest.resolve()
    with tarfile.open(path, "r") as tar:
        for entry in manifest.files:
            target = (root / entry.path).resolve()
            if root not in target.parents:
                raise BundleError(f"unsafe path in the bundle: {entry.path[:80]!r}")
            if target.exists():
                raise BundleError(f"{entry.path} already exists in the destination")
            target.parent.mkdir(parents=True, exist_ok=True)
            fh = tar.extractfile(entry.path)
            if fh is None:  # pragma: no cover - verified above
                raise BundleError(f"{entry.path} cannot be read")
            with target.open("wb") as out:
                while chunk := fh.read(_CHUNK):
                    out.write(chunk)
    return manifest
