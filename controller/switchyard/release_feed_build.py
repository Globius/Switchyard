# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Build and sign the ``releases.json`` that controllers read (ADR 0205).

Used by `syctl release feed` in the release process. The feed lists each release with its notes,
the versions it installs over, its agent compatibility and (when the bundle is published) the
bundle's URL, SHA-256 and size, all taken from the signed bundle itself so the two cannot disagree.
A feed built from an earlier one keeps its other releases, so a new release is one more entry.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from switchyard.domain.bundle import BundleError, Manifest
from switchyard.domain.releases import (
    Channel,
    FeedError,
    ReleaseFeed,
    public_key_b64,
    sign_feed,
    verify_feed,
)
from switchyard.domain.versions import UNKNOWN, version_key
from switchyard.services.release_bundle import verify


def release_entry(
    manifest: Manifest, bundle: Path, url: str, channel: Channel, notes_url: str | None
) -> dict[str, Any]:
    """The feed entry for one verified bundle.

    An entry carries what a controller shows and checks: `version`, `channel`, `date`, `notes`,
    `upgrade_from`, `min_agent` and the `bundle` reference (URL, SHA-256, size).
    """
    data = bundle.read_bytes()
    entry: dict[str, Any] = {
        "version": manifest.version,
        "channel": channel,
        "date": manifest.created_at[:10],
        "notes": manifest.notes.model_dump(),
        "upgrade_from": list(manifest.upgrade_from),
        "min_agent": manifest.min_agent,
        "recommended_min_agent": manifest.recommended_min_agent,
        "bundle": {
            "url": url,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        },
    }
    if notes_url:
        entry["notes_url"] = notes_url
    return entry


def build_feed(
    key: Ed25519PrivateKey,
    key_id: str,
    bundles: list[tuple[Path, str]],
    *,
    channel: Channel = "stable",
    notes_url: str | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A signed feed document with the ``bundles`` (path and published URL) added to ``previous``.

    Each bundle must be signed by ``key``; an earlier feed must verify with it too, so a feed
    signed by somebody else is never silently re-signed.

    Raises:
        BundleError: A bundle does not verify against ``key``.
        FeedError: ``previous`` does not verify against ``key``.
    """
    trusted = {key_id: public_key_b64(key)}
    releases: dict[str, dict[str, Any]] = {}
    if previous is not None:
        old = verify_feed(previous, trusted)
        releases = {r.version: r.model_dump(mode="json", exclude_none=True) for r in old.releases}
    for path, url in bundles:
        manifest = verify(path, trusted)
        releases[manifest.version] = release_entry(manifest, path, url, channel, notes_url)
    payload = {
        "schema": 1,
        "releases": sorted(releases.values(), key=lambda r: version_key(r["version"]) or UNKNOWN),
    }
    ReleaseFeed.model_validate(payload)  # never sign something controllers would reject
    return sign_feed(payload, key, key_id)


def load_previous(path: Path) -> dict[str, Any]:
    """Read an earlier feed file."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise FeedError(f"cannot read the earlier feed {path}") from exc
    if not isinstance(doc, dict):
        raise FeedError("the earlier feed is not a signed document")
    return doc


__all__ = ["BundleError", "FeedError", "build_feed", "load_previous", "release_entry"]
