# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The release feed: where to look for newer versions, and what was found (ADR 0205).

Off until a system administrator sets a feed URL. The controller does one plain HTTPS GET (no
redirects, no cookies, nothing about this installation in the request), verifies the Ed25519
signature against the trusted keys, and stores the result. A fetch that fails, is oversized or does
not verify is recorded as an error and changes nothing else; it never stops the controller.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from switchyard.db import audit_log
from switchyard.db.auth_models import SystemSettingRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.releases import Channel, FeedError, ReleaseFeed, latest, verify_feed
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import FieldErrors

CONFIG_KEY = "release_feed"
STATE_KEY = "release_feed_state"
CHECK_RELEASES_JOB = "check_releases"
MAX_FEED_BYTES = 1024 * 1024
#: Public keys the project signs releases with (key id to base64 Ed25519 public key, 32 raw bytes).
#: `release-2026` is the maintainers' offline key. Add `ci-release-2026` when it exists (the key
#: CI signs tagged releases with). The nightly key is deliberately NOT here: operators who want
#: nightlies trust it themselves.
PROJECT_KEYS: dict[str, str] = {
    "release-2026": "Lr/O9yFbtWHxGa/ZO/U1ASnRHuFHFYu/TJka8C/o6do=",
}


@dataclass(frozen=True)
class FeedConfig:
    """Operator settings for the feed."""

    url: str = ""
    channel: Channel = "stable"
    enabled: bool = False
    trusted_keys: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class FeedState:
    """What the last fetch found."""

    checked_at: str | None = None
    ok: bool | None = None
    error: str | None = None
    latest_version: str | None = None
    releases: list[dict[str, Any]] = field(default_factory=list)


def _check_config(body: dict[str, Any]) -> FeedConfig:
    errors = FieldErrors()
    url = str(body.get("url", ""))
    parts = urlsplit(url)
    if url and (parts.scheme != "https" or not parts.hostname or parts.username or parts.password):
        errors.add("url", "invalid", "must be an https URL without credentials")
    channel = body.get("channel", "stable")
    if channel not in ("stable", "candidate", "nightly"):
        errors.add("channel", "invalid", "must be stable, candidate or nightly")
    keys = body.get("trusted_keys") or {}
    if not isinstance(keys, dict) or len(keys) > 10:
        errors.add("trusted_keys", "invalid", "must be an object of at most 10 key ids")
    else:
        for kid, value in keys.items():
            if not (isinstance(kid, str) and 0 < len(kid) <= 64 and isinstance(value, str)):
                errors.add("trusted_keys", "invalid", "key ids and base64 public keys are strings")
                break
            if len(value) != 44:
                errors.add("trusted_keys", "invalid", f"{kid}: not a 32-byte base64 public key")
    if bool(body.get("enabled")) and not url:
        errors.add("url", "required", "a feed URL is needed to enable the check")
    errors.raise_if_any()
    return FeedConfig(url, channel, bool(body.get("enabled")), dict(keys))


class ReleaseFeedService:
    """Configure the feed, fetch it, and report what it says."""

    def __init__(self, db: Databases, client: httpx.AsyncClient | None = None) -> None:
        """``client`` is replaced in tests."""
        self._db = db
        self._client = client

    async def _get(self, key: str) -> Any:
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(select(SystemSettingRow).where(SystemSettingRow.key == key))
            ).scalar_one_or_none()
        return None if row is None else row.value

    async def _put(self, key: str, value: Any, actor: str, session: Any = None) -> None:
        async def write(s: Any) -> None:
            row = await s.get(SystemSettingRow, key)
            if row is None:
                s.add(SystemSettingRow(key=key, value=value, updated_by=actor))
            else:
                row.value, row.updated_by = value, actor

        if session is not None:
            await write(session)
            return
        async with admin_session(self._db.admin) as s:
            await write(s)

    async def config(self, principal: Principal | None = None) -> FeedConfig:
        """The current settings (a permission check when ``principal`` is given)."""
        if principal is not None:
            require(principal, "manage", "controller")
        raw = await self._get(CONFIG_KEY) or {}
        return FeedConfig(
            raw.get("url", ""),
            raw.get("channel", "stable"),
            bool(raw.get("enabled", False)),
            dict(raw.get("trusted_keys", {})),
        )

    async def set_config(
        self, principal: Principal, body: dict[str, Any], *, request_id: str | None = None
    ) -> FeedConfig:
        """Replace the settings. Raises :class:`FieldErrors` for invalid ones."""
        require(principal, "manage", "controller")
        cfg = _check_config(body)
        async with admin_session(self._db.admin) as session:
            await self._put(
                CONFIG_KEY,
                {
                    "url": cfg.url,
                    "channel": cfg.channel,
                    "enabled": cfg.enabled,
                    "trusted_keys": cfg.trusted_keys,
                },
                principal.actor,
                session,
            )
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="release_feed.configured",
                object_ref="setting:release_feed",
                diff={"enabled": cfg.enabled, "channel": cfg.channel, "url": cfg.url},
                request_id=request_id,
            )
        return cfg

    async def state(self) -> FeedState:
        """What the last fetch found (no permission check; the upgrade check calls it)."""
        raw = await self._get(STATE_KEY) or {}
        return FeedState(
            raw.get("checked_at"),
            raw.get("ok"),
            raw.get("error"),
            raw.get("latest_version"),
            list(raw.get("releases", [])),
        )

    async def feed(self) -> ReleaseFeed | None:
        """The last verified feed, or ``None``."""
        state = await self.state()
        if not state.ok:
            return None
        return ReleaseFeed.model_validate({"schema": 1, "releases": state.releases})

    async def _fetch(self, url: str) -> bytes:
        client = self._client or httpx.AsyncClient(timeout=20.0, follow_redirects=False)
        try:
            async with client.stream("GET", url, headers={"accept": "application/json"}) as res:
                if res.status_code != 200:
                    raise FeedError(f"the feed server answered HTTP {res.status_code}")
                body = b""
                async for chunk in res.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_FEED_BYTES:
                        raise FeedError("the feed is larger than 1 MiB")
                return body
        except httpx.HTTPError as exc:
            raise FeedError(f"the feed could not be fetched ({type(exc).__name__})") from exc
        finally:
            if self._client is None:
                await client.aclose()

    async def refresh(self, principal: Principal | None = None) -> FeedState:
        """Fetch and verify the feed now. The outcome is stored either way.

        With a ``principal``, a system administrator is required (the "Check now" button); the
        scheduled job passes none. Does nothing, and returns the stored state, when disabled and
        called by the schedule.
        """
        if principal is not None:
            require(principal, "manage", "controller")
        cfg = await self.config()
        if not cfg.url or (principal is None and not cfg.enabled):
            return await self.state()
        stamp = datetime.now(UTC).isoformat()
        try:
            doc = json.loads(await self._fetch(cfg.url))
            feed = verify_feed(doc, {**PROJECT_KEYS, **cfg.trusted_keys})
            top = latest(feed, cfg.channel)
            value: dict[str, Any] = {
                "checked_at": stamp,
                "ok": True,
                "error": None,
                "latest_version": top.version if top else None,
                "releases": [r.model_dump(mode="json") for r in feed.releases],
            }
        except (FeedError, ValueError) as exc:
            previous = await self._get(STATE_KEY) or {}
            value = {
                **previous,
                "checked_at": stamp,
                "ok": False if not previous.get("releases") else previous.get("ok"),
                "error": str(exc)[:300],
            }
        await self._put(STATE_KEY, value, principal.actor if principal else "system:release_feed")
        return await self.state()
