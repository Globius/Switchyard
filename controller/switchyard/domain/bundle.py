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
from dataclasses import dataclass
from typing import Annotated, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from switchyard.domain.releases import ReleaseNotes
from switchyard.domain.versions import version_key

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
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]{1,32})?$")]
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
    return parse_manifest(raw)


def parse_manifest(raw: bytes) -> Manifest:
    """Parse and check a manifest without looking at any signature.

    Used on its own only for an explicitly unsigned bundle (`--allow-unsigned`); every other caller
    goes through :func:`verify_manifest`. Checks the schema, every path, and duplicates.

    Raises:
        BundleError: The manifest is not valid.
    """
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
    return manifest


@dataclass(frozen=True)
class Plan:
    """What applying a bundle involves, for the operator to read before doing it."""

    steps: list[str]
    warnings: list[str]


def plan(manifest: Manifest, running: str) -> Plan:
    """The steps and cautions for installing ``manifest`` over the version ``running``.

    Never refuses: a bundle that does not list ``running`` in ``upgrade_from`` gets a warning,
    not a block.

    Example:
        >>> m = Manifest(version="0.2.0", schema_head="0046", created_at="x",
        ...              upgrade_from=["0.1.0"],
        ...              files=[BundleFile(path="files/compose/c.yaml", kind="compose",
        ...                                sha256="0" * 64, size_bytes=1)])
        >>> plan(m, "0.1.0").warnings
        []
        >>> plan(m, "0.0.5").warnings[0].startswith("This bundle installs over 0.1.0")
        True
    """
    warnings: list[str] = []
    have, want = version_key(running), version_key(manifest.version)
    if have is not None and want is not None and have >= want:
        warnings.append(
            f"This controller already runs {running}, which is not older than the bundle."
        )
    if manifest.upgrade_from and running not in manifest.upgrade_from:
        warnings.append(
            f"This bundle installs over {', '.join(manifest.upgrade_from)}; you run {running}. "
            "Go through the intermediate releases first."
        )
    kinds = {f.kind for f in manifest.files}
    steps = [
        "Back up the configuration (Back up now) and the database (pgBackRest).",
        "Load the controller and proxy images on each controller node.",
        "Run the schema migration once; migrations are additive, so the old code keeps working.",
        "Upgrade controller nodes one at a time (syctl upgrade apply), checking health after each.",
    ]
    if "helm-chart" in kinds:
        steps.append(
            "On Kubernetes use helm upgrade with the bundled chart instead of the steps above."
        )
    if "se-template" in kinds or "agent" in kinds:
        steps.append("Roll each SE group to the new template, one engine at a time (Engine rolls).")
    return Plan(steps, warnings)
