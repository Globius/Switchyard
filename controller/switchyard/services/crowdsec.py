# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The CrowdSec integration: settings, status, a connection test and "sync now" (ADR 0213).

The settings live in ``system_setting`` (key ``crowdsec``) and are changed by a system
administrator; ``SWITCHYARD_CROWDSEC_URL``, ``_API_KEY`` and ``_ORIGINS`` in the environment are the
fallback while nothing is stored. The bouncer key is never stored here: it goes to the secrets
backend and only its reference (``key_ref``) is kept and shown. Each time the sync job reads the
decision stream the outcome (time, number of bans, error) is recorded in ``crowdsec_state``.

Example:
    >>> normalize_origins(" crowdsec, CAPI ,,")
    ['crowdsec', 'CAPI']
"""

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from switchyard.adapters.crowdsec import CrowdSec, CrowdSecError, check_address, check_url
from switchyard.adapters.iplists import IpListError
from switchyard.adapters.secrets import SecretNotFoundError, SecretsReader
from switchyard.db import audit_log
from switchyard.db.auth_models import SystemSettingRow
from switchyard.db.crowdsec_config import (
    CONFIG_KEY,
    CrowdSecConfig,
    get_setting,
    load_config,
    normalize_origins,
)
from switchyard.db.tenancy import admin_session
from switchyard.domain import validators as v
from switchyard.jobs import queue
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import FieldErrors

log = logging.getLogger("switchyard.crowdsec")

STATE_KEY = "crowdsec_state"
#: Name of the reputation feed that this integration provides.
CROWDSEC_FEED = "crowdsec"
#: Where a pasted bouncer key is stored.
DEFAULT_KEY_REF = "kv/system/crowdsec/bouncer-key"
#: The job that keeps managed IP lists current (see :mod:`switchyard.services.iplists`).
SYNC_JOB = "sync_ip_lists"
_MAX_ORIGINS = 20


@dataclass(frozen=True)
class CrowdSecState:
    """What the last read of the decision stream found."""

    synced_at: str | None = None
    ok: bool | None = None
    bans: int | None = None
    error: str | None = None


def _check(body: dict[str, Any], writable: bool) -> tuple[str, str, list[str], str | None, str]:
    errors = FieldErrors()
    url = str(body.get("url") or "")
    if url:
        try:
            url = check_url(url)
        except ValueError as exc:
            errors.add("url", "invalid", str(exc))
    key_ref = str(body.get("key_ref") or "")
    if key_ref:
        try:
            v.validate_secret_ref(key_ref)
        except ValueError as exc:
            errors.add("key_ref", "invalid", str(exc))
    api_key = body.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        errors.add("api_key", "invalid", "must be a string")
        api_key = None
    if api_key and not writable:
        errors.add("api_key", "unavailable", "needs a writable secrets backend (OpenBao)")
    origins = normalize_origins(body.get("origins"))
    if len(origins) > _MAX_ORIGINS or any(len(o) > 64 for o in origins):
        errors.add("origins", "invalid", f"at most {_MAX_ORIGINS} names of up to 64 characters")
    logs_address = str(body.get("logs_address") or "")
    if logs_address:
        try:
            logs_address = check_address(logs_address)
        except ValueError as exc:
            errors.add("logs_address", "invalid", str(exc))
    errors.raise_if_any()
    return url, key_ref, origins, api_key or None, logs_address


class CrowdSecIntegration:
    """Configure the integration, read the decision stream, and report how it is going."""

    def __init__(
        self,
        db: Databases,
        client: CrowdSec,
        secrets: SecretsReader | None = None,
    ) -> None:
        """Create the service.

        Args:
            db: Database handles.
            client: The CrowdSec local API client.
            secrets: Resolves ``key_ref``; if it can also write (``put``), a pasted key is stored.
        """
        self._db = db
        self._client = client
        self._secrets = secrets

    async def _get(self, key: str) -> Any:
        return await get_setting(self._db.admin, key)

    async def _put(self, session: Any, key: str, value: Any, actor: str) -> None:
        row = await session.get(SystemSettingRow, key)
        if row is None:
            session.add(SystemSettingRow(key=key, value=value, updated_by=actor))
        else:
            row.value, row.updated_by = value, actor

    async def config(self, principal: Principal | None = None) -> CrowdSecConfig:
        """The effective settings (a system-admin check when ``principal`` is given)."""
        if principal is not None:
            require(principal, "manage", "controller")
        return await load_config(self._db.admin)

    async def set_config(
        self, principal: Principal, body: dict[str, Any], *, request_id: str | None = None
    ) -> CrowdSecConfig:
        """Replace the stored settings (system administrators only).

        ``api_key`` is written to the secrets backend at :data:`DEFAULT_KEY_REF` and only the
        reference is kept; ``key_ref`` points at a key somebody stored there already. Omitting both
        keeps the current reference. An empty ``url`` removes the stored settings, which returns to
        the environment fallback.

        Raises:
            FieldErrors: For invalid settings.
            PermissionDeniedError: If the principal is not a system administrator.
        """
        require(principal, "manage", "controller")
        writer: Any = self._secrets if hasattr(self._secrets, "put") else None
        url, key_ref, origins, api_key, logs_address = _check(body, writable=writer is not None)
        previous = await self._get(CONFIG_KEY) or {}
        if api_key:
            await writer.put(DEFAULT_KEY_REF, api_key)
            key_ref = DEFAULT_KEY_REF
        elif not key_ref:
            key_ref = previous.get("key_ref", "")
        stored = {
            "url": url,
            "key_ref": key_ref if url else "",
            "origins": origins,
            "logs_address": logs_address if url else "",
        }
        async with admin_session(self._db.admin) as session:
            await self._put(session, CONFIG_KEY, stored, principal.actor)
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="crowdsec.configured",
                object_ref="setting:crowdsec",
                diff={
                    "url": url,
                    "origins": origins,
                    "logs_address": logs_address,
                    "key_changed": bool(api_key) or None,
                },
                request_id=request_id,
            )
        return await self.config()

    async def state(self) -> CrowdSecState:
        """What the last read found (no permission check)."""
        raw = await self._get(STATE_KEY) or {}
        return CrowdSecState(raw.get("synced_at"), raw.get("ok"), raw.get("bans"), raw.get("error"))

    async def _key(self, cfg: CrowdSecConfig) -> str:
        if cfg.key_ref:
            if self._secrets is None:
                raise CrowdSecError("no secrets backend is configured to read the bouncer key")
            try:
                return await self._secrets.get(cfg.key_ref)
            except SecretNotFoundError as exc:
                raise CrowdSecError(f"the bouncer key {cfg.key_ref} was not found") from exc
        key = os.environ.get("SWITCHYARD_CROWDSEC_API_KEY") or ""
        if not key:
            raise CrowdSecError("no bouncer key is set")
        return key

    async def _read(self) -> list[str]:
        cfg = await self.config()
        if not cfg.configured:
            raise CrowdSecError("CrowdSec is not configured")
        key = await self._key(cfg)
        # The community blocklist (origin CAPI) is for the engines that subscribe to it. Copying it
        # into tenant lists (and the Git mirror) is not something its terms were checked for, so it
        # stays out unless an administrator names CAPI in the origins (ADR 0215).
        exclude = frozenset() if cfg.origins else frozenset({"CAPI"})
        return await self._client.decisions(cfg.url, key, frozenset(cfg.origins) or None, exclude)

    async def networks(self) -> list[str]:
        """The banned networks, for the sync job. Records the outcome either way.

        Raises:
            IpListError: If CrowdSec is not configured or the read failed; the list stays as it is.
        """
        stamp = datetime.now(UTC).isoformat()
        try:
            nets = await self._read()
        except IpListError as exc:
            await self._record({"synced_at": stamp, "ok": False, "bans": None, "error": str(exc)})
            raise
        await self._record({"synced_at": stamp, "ok": True, "bans": len(nets), "error": None})
        return nets

    async def _record(self, value: dict[str, Any]) -> None:
        previous = await self._get(STATE_KEY) or {}
        if not value["ok"]:  # keep the last known count when a read fails
            value = {**value, "bans": previous.get("bans")}
        async with admin_session(self._db.admin) as session:
            await self._put(session, STATE_KEY, value, "system:crowdsec")

    async def test(self, principal: Principal) -> dict[str, Any]:
        """Read the stream once with the stored settings and report. Changes nothing.

        Returns ``{"ok": bool, "bans": int | None, "error": str | None}``. A system administrator
        is required.
        """
        require(principal, "manage", "controller")
        try:
            nets = await self._read()
        except IpListError as exc:
            return {"ok": False, "bans": None, "error": str(exc)[:300]}
        return {"ok": True, "bans": len(nets), "error": None}

    async def sync_now(self, principal: Principal, *, request_id: str | None = None) -> str:
        """Queue the IP list sync job now and return the job id (system administrators only)."""
        require(principal, "manage", "controller")
        async with admin_session(self._db.admin) as session:
            job_id = await queue.enqueue(
                session, SYNC_JOB, {}, max_attempts=1, request_id=request_id
            )
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="crowdsec.sync_requested",
                object_ref="setting:crowdsec",
                diff={"job_id": str(job_id)},
                request_id=request_id,
            )
        return str(job_id)
