# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The upgrade bundle manifest: what a release archive contains and how it is verified (ADR 0205).

Pure models and checks, no I/O. A bundle is a tar archive holding ``manifest.json``,
``manifest.sig`` and the files the manifest lists under ``files/``. The signature is Ed25519 over
the exact bytes of ``manifest.json``; the manifest lists every file with its SHA-256, so one
signature covers the whole archive.

Example:
    >>> m = Manifest(version="0.2.0", schema_head="0046", created_at="2026-10-13T04:00:00Z",
    ...              files=[BundleFile(path="files/compose/compose.yaml", kind="compose",
    ...                                sha256="0" * 64, size_bytes=10)])
    >>> check_member_path("files/compose/compose.yaml")
    'files/compose/compose.yaml'
"""

import base64
import json
import re
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from switchyard.domain.releases import ReleaseNotes

FORMAT: Literal[1] = 1
MANIFEST = "manifest.json"
SIGNATURE = "manifest.sig"
Kind = Literal[
    "controller-image", "proxy-image", "helm-chart", "compose", "agent", "se-template", "docs"
]
_PATH = re.compile(r"^files/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


class BundleError(Exception):
    """The bundle cannot be trusted or is malformed. Messages never include file contents."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BundleFile(_Strict):
    """One file in the archive."""

    path: Annotated[str, Field(pattern=_PATH.pattern, max_length=200)]
    kind: Kind
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: Annotated[int, Field(ge=0)]


class Manifest(_Strict):
    """What a bundle is: the release, its schema, what it can be installed over, and its files."""

    format: Literal[1] = FORMAT
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]{1,16})?$")]
    schema_head: Annotated[str, Field(pattern=r"^[0-9a-z_]{1,40}$")]
    created_at: str
    upgrade_from: list[str] = Field(default_factory=list, max_length=100)
    min_agent: str = "0.0.0"
    recommended_min_agent: str = "0.0.0"
    notes: ReleaseNotes = Field(default_factory=ReleaseNotes)
    files: list[BundleFile] = Field(max_length=200)


def check_member_path(name: str) -> str:
    """Return ``name`` if it is a safe archive path under ``files/``, else raise.

    Raises:
        BundleError: Absolute, parent-relative, empty-segment or oddly named paths.
    """
    if not _PATH.match(name) or ".." in name.split("/"):
        raise BundleError(f"unsafe path in the bundle: {name[:80]!r}")
    return name


def manifest_bytes(manifest: Manifest) -> bytes:
    """Canonical bytes of the manifest (what is signed and written)."""
    return json.dumps(
        manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


def sign_manifest(raw: bytes, key: Ed25519PrivateKey, key_id: str) -> bytes:
    """The ``manifest.sig`` content for ``raw`` (the manifest bytes)."""
    return json.dumps(
        {"key_id": key_id, "signature": base64.b64encode(key.sign(raw)).decode()}
    ).encode()


def verify_manifest(raw: bytes, sig: bytes, trusted: dict[str, str]) -> Manifest:
    """Verify ``raw`` against ``sig`` and the trusted keys (id to base64 public key), then parse it.

    Raises:
        BundleError: Unknown key, bad signature or an invalid manifest.
    """
    try:
        doc = json.loads(sig)
        key_id, signature = str(doc["key_id"]), str(doc["signature"])
    except (ValueError, KeyError, TypeError) as exc:
        raise BundleError("the signature file is malformed") from exc
    if key_id not in trusted:
        raise BundleError(f"the bundle is signed with a key that is not trusted ({key_id[:64]})")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(trusted[key_id])).verify(
            base64.b64decode(signature, validate=True), raw
        )
    except (InvalidSignature, ValueError) as exc:
        raise BundleError("the manifest signature does not verify") from exc
    try:
        manifest = Manifest.model_validate_json(raw)
    except ValidationError as exc:
        raise BundleError("the manifest is not valid") from exc
    seen: set[str] = set()
    for f in manifest.files:
        check_member_path(f.path)
        if f.path in seen:
            raise BundleError(f"{f.path} is listed twice")
        seen.add(f.path)
    return manifest
