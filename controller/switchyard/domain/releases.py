# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The release feed: a signed list of releases, and what to do about it (ADR 0205).

Pure functions and models, no I/O. A feed document is ``{"payload": {...}, "signature": ...,
"key_id": ...}``; the signature is Ed25519 over the canonical JSON of ``payload`` (sorted keys, no
spaces). A feed whose signature does not verify against a trusted key is never used, whoever
hosts it. The feed only *informs*: nothing is downloaded or run because a feed says so.

Example:
    >>> from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    >>> key = Ed25519PrivateKey.generate()
    >>> payload = {"schema": 1, "releases": []}
    >>> doc = sign_feed(payload, key, "k1")
    >>> verify_feed(doc, {"k1": public_key_b64(key)}).releases
    []
"""

import base64
import json
import re
from typing import Annotated, Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from switchyard.domain.versions import parse_version

Channel = Literal["stable", "candidate"]
_VERSION = r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]{1,16})?$"


class FeedError(Exception):
    """The feed could not be trusted or understood. The message never includes feed content."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReleaseNotes(_Strict):
    """What a release changes, in the four groups an operator reads before confirming."""

    new: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=50)
    deprecated: list[Annotated[str, Field(max_length=300)]] = Field(
        default_factory=list, max_length=50
    )
    breaking: list[Annotated[str, Field(max_length=300)]] = Field(
        default_factory=list, max_length=50
    )
    action_required: list[Annotated[str, Field(max_length=300)]] = Field(
        default_factory=list, max_length=50
    )


class BundleRef(_Strict):
    """Where to fetch an upgrade bundle and what it must hash to."""

    url: Annotated[str, Field(pattern=r"^https://[^\s]{1,500}$")]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    size_bytes: Annotated[int, Field(ge=1)]


class Release(_Strict):
    """One release in a feed."""

    version: Annotated[str, Field(pattern=_VERSION)]
    channel: Channel = "stable"
    date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]
    notes_url: Annotated[str, Field(pattern=r"^https://[^\s]{1,500}$")] | None = None
    notes: ReleaseNotes = Field(default_factory=ReleaseNotes)
    upgrade_from: list[Annotated[str, Field(pattern=_VERSION)]] = Field(
        default_factory=list, max_length=100
    )
    min_agent: Annotated[str, Field(pattern=_VERSION)] = "0.0.0"
    recommended_min_agent: Annotated[str, Field(pattern=_VERSION)] = "0.0.0"
    bundle: BundleRef | None = None


class ReleaseFeed(_Strict):
    """The payload of a feed."""

    schema_: Literal[1] = Field(alias="schema")
    releases: list[Release] = Field(max_length=500)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def canonical(payload: dict[str, Any]) -> bytes:
    """The bytes that are signed: sorted keys, no whitespace, UTF-8."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def public_key_b64(key: Ed25519PrivateKey) -> str:
    """The raw public key, base64, as stored in trusted-key settings."""
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign_feed(payload: dict[str, Any], key: Ed25519PrivateKey, key_id: str) -> dict[str, Any]:
    """Build a feed document (used by the release process and the tests)."""
    return {
        "payload": payload,
        "key_id": key_id,
        "signature": base64.b64encode(key.sign(canonical(payload))).decode(),
    }


_KEY_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def verify_feed(doc: object, trusted: dict[str, str]) -> ReleaseFeed:
    """Check the signature against ``trusted`` (key id to base64 public key) and parse the payload.

    Raises:
        FeedError: No trusted key, unknown key id, bad signature or an invalid payload.
    """
    if not isinstance(doc, dict) or set(doc) != {"payload", "signature", "key_id"}:
        raise FeedError("the feed is not a signed document")
    key_id, signature, payload = doc["key_id"], doc["signature"], doc["payload"]
    if not isinstance(key_id, str) or not _KEY_ID.match(key_id) or not isinstance(payload, dict):
        raise FeedError("the feed is not a signed document")
    if key_id not in trusted:
        raise FeedError(f"the feed is signed with a key that is not trusted ({key_id})")
    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(trusted[key_id]))
        public.verify(base64.b64decode(str(signature), validate=True), canonical(payload))
    except (InvalidSignature, ValueError) as exc:
        raise FeedError("the feed signature does not verify") from exc
    try:
        return ReleaseFeed.model_validate(payload)
    except ValidationError as exc:
        raise FeedError("the signed payload is not a valid release feed") from exc


def latest(feed: ReleaseFeed, channel: Channel = "stable") -> Release | None:
    """The highest version on ``channel`` (``candidate`` also considers stable releases).

    Example:
        >>> feed = ReleaseFeed.model_validate({"schema": 1, "releases": [
        ...     {"version": "0.2.0", "date": "2026-10-01"},
        ...     {"version": "0.3.0-rc1", "channel": "candidate", "date": "2026-10-10"}]})
        >>> latest(feed).version, latest(feed, "candidate").version
        ('0.2.0', '0.3.0-rc1')
    """
    allowed = {"stable"} if channel == "stable" else {"stable", "candidate"}
    best: Release | None = None
    best_key: tuple[int, int, int, int] | None = None
    for r in feed.releases:
        v = parse_version(r.version)
        if v is None or r.channel not in allowed:
            continue
        key = (*v, 0 if "-" in r.version else 1)
        if best_key is None or key > best_key:
            best, best_key = r, key
    return best


def upgrade_path(feed: ReleaseFeed, current: str, target: str) -> list[str]:
    """The versions to install, in order, to get from ``current`` to ``target``.

    Each release lists the versions it can be installed over (``upgrade_from``); an empty list
    means "from any". The path is the shortest chain, found backwards from the target. A direct
    upgrade is a one-element path. Returns ``[]`` when ``current`` is already at or past
    ``target``, and ``[target]`` when no chain is documented (the caller shows a warning, it
    does not block).

    Example:
        >>> feed = ReleaseFeed.model_validate({"schema": 1, "releases": [
        ...     {"version": "0.2.0", "date": "2026-01-01", "upgrade_from": ["0.1.0"]},
        ...     {"version": "0.3.0", "date": "2026-02-01", "upgrade_from": ["0.2.0"]}]})
        >>> upgrade_path(feed, "0.1.0", "0.3.0")
        ['0.2.0', '0.3.0']
    """
    cur, tgt = parse_version(current), parse_version(target)
    if cur is None or tgt is None or cur >= tgt:
        return []
    by_version = {r.version: r for r in feed.releases}
    path: list[str] = [target]
    seen = {target}
    while True:
        release = by_version.get(path[0])
        if release is None or not release.upgrade_from or current in release.upgrade_from:
            return path
        # the highest listed predecessor that is itself newer than the current version
        steps = sorted(
            (v for v in release.upgrade_from if (p := parse_version(v)) and p > cur),
            key=lambda v: parse_version(v) or (0, 0, 0),
            reverse=True,
        )
        nxt = next((v for v in steps if v not in seen), None)
        if nxt is None:
            return [target]
        path.insert(0, nxt)
        seen.add(nxt)
