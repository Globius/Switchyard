# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Administration of the break-glass administrator (system administrators; ADR 0032).

The break-glass login (password plus TOTP, see :mod:`switchyard.services.sessions`) is for the day
every identity provider is down. It is **on** when the deployment forces it
(``SWITCHYARD_BREAK_GLASS=1``) or when a system administrator switched it on for a limited time
with a reason. Enrolment generates the password and the TOTP secret, stores the secret in the
secrets backend and shows both exactly once; the administrator cannot sign in until a valid code
confirms that the authenticator was set up.
"""

import base64
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

from sqlalchemy import delete, select

from switchyard.adapters.secrets import SecretsReader, SecretsWriter
from switchyard.db import audit_log
from switchyard.db.auth_models import BreakGlassSettingRow, LocalAdminRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.validators import validate_name
from switchyard.services.changesets import require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.errors import ConflictError, FieldErrors, NotFoundError
from switchyard.services.sessions import hash_password, verify_totp

DEFAULT_TTL_MINUTES = 240
MAX_TTL_MINUTES = 1440
DEFAULT_USERNAME = "breakglass"
ISSUER = "Switchyard"


@dataclass(frozen=True)
class BreakGlassAdmin:
    """The enrolled administrator (never its password or secret)."""

    username: str
    totp_confirmed: bool
    created_at: datetime
    last_login_at: datetime | None
    locked_until: datetime | None


@dataclass(frozen=True)
class BreakGlassStatus:
    """Whether break-glass login is on, why, and who the administrator is."""

    forced_by_deployment: bool
    enabled: bool
    enabled_until: datetime | None
    enabled_by: str | None
    reason: str | None
    admin: BreakGlassAdmin | None


@dataclass(frozen=True)
class Enrollment:
    """What enrolment shows once: password, TOTP secret and its ``otpauth://`` URI."""

    username: str
    password: str
    totp_secret: str
    otpauth_uri: str


def otpauth_uri(username: str, secret: str) -> str:
    """The URI an authenticator app reads from the QR code.

    Example:
        >>> otpauth_uri("breakglass", "ABC")
        'otpauth://totp/Switchyard:breakglass?secret=ABC&issuer=Switchyard&algorithm=SHA1&digits=6&period=30'
    """
    query = urlencode(
        {"secret": secret, "issuer": ISSUER, "algorithm": "SHA1", "digits": 6, "period": 30}
    )
    return f"otpauth://totp/{ISSUER}:{quote(username)}?{query}"


class BreakGlassService:
    """Enrol, confirm and remove the administrator; switch break-glass login on and off."""

    def __init__(
        self,
        db: Databases,
        secrets_reader: SecretsReader | None = None,
        secrets_writer: SecretsWriter | None = None,
        *,
        forced: bool = False,
        clock: Clock = utcnow,
    ) -> None:
        """Create the service.

        Args:
            db: Database handles.
            secrets_reader: Reads the TOTP secret when confirming.
            secrets_writer: Stores the TOTP secret at enrolment.
            forced: True when ``SWITCHYARD_BREAK_GLASS=1``; the API cannot turn that off.
            clock: Time source.
        """
        self._db = db
        self._reader = secrets_reader
        self._writer = secrets_writer
        self._forced = forced
        self._clock = clock

    async def status(self, principal: Principal) -> BreakGlassStatus:
        """The current state (system administrators)."""
        require(principal, "manage", "break_glass")
        async with admin_session(self._db.admin) as session:
            return await self._status(session)

    async def set_enabled(
        self,
        principal: Principal,
        *,
        enabled: bool,
        reason: str | None,
        ttl_minutes: int | None,
        request_id: str | None = None,
    ) -> BreakGlassStatus:
        """Switch break-glass login on for ``ttl_minutes`` (default 240, at most 1440) or off.

        Raises:
            FieldErrors: If enabling without a reason, or with a lifetime out of range.
            ConflictError: If enabling without a confirmed administrator.
        """
        require(principal, "manage", "break_glass")
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            if enabled:
                errors = FieldErrors()
                if not reason or not reason.strip():
                    errors.add(
                        "reason", "required", "say why break-glass login is being switched on"
                    )
                ttl = ttl_minutes if ttl_minutes is not None else DEFAULT_TTL_MINUTES
                if not 1 <= ttl <= MAX_TTL_MINUTES:
                    errors.add(
                        "ttl_minutes", "out_of_range", f"must be between 1 and {MAX_TTL_MINUTES}"
                    )
                errors.raise_if_any()
                admin = (await session.execute(select(LocalAdminRow))).scalars().first()
                if admin is None or admin.totp_confirmed_at is None:
                    raise ConflictError(
                        "enrol the break-glass administrator and confirm its TOTP code first"
                    )
                until = now + timedelta(minutes=ttl)
                await self._store(session, until, principal.actor, now, (reason or "").strip())
                await audit_log.record(
                    session,
                    tenant_id=None,
                    actor=principal.actor,
                    action="break_glass.enabled",
                    diff={"until": until.isoformat(), "reason": (reason or "").strip()},
                    request_id=request_id,
                )
            else:
                await self._store(session, None, None, None, None)
                await audit_log.record(
                    session,
                    tenant_id=None,
                    actor=principal.actor,
                    action="break_glass.disabled",
                    diff={"forced_by_deployment": self._forced},
                    request_id=request_id,
                )
            await session.flush()
            return await self._status(session)

    async def create_admin(
        self,
        principal: Principal,
        *,
        username: str = DEFAULT_USERNAME,
        request_id: str | None = None,
    ) -> Enrollment:
        """Create or replace the administrator; shows the password and TOTP secret once.

        The previous administrator, if any, is removed and break-glass login is switched off:
        the new one must be confirmed before it can be used.

        Raises:
            FieldErrors: For a malformed username.
            ConflictError: If the controller has no secrets backend to keep the TOTP secret.
        """
        require(principal, "manage", "break_glass")
        errors = FieldErrors()
        try:
            validate_name(username)
        except ValueError as exc:
            errors.add("username", "invalid", str(exc))
        errors.raise_if_any()
        if self._writer is None:
            raise ConflictError("the controller has no secrets backend to keep the TOTP secret")
        password = secrets.token_urlsafe(18)
        totp_secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")
        ref = f"kv/break-glass/{username}-totp"
        await self._writer.put(ref, totp_secret)
        async with admin_session(self._db.admin) as session:
            await session.execute(delete(LocalAdminRow))
            await self._store(session, None, None, None, None)
            session.add(
                LocalAdminRow(
                    username=username,
                    password_hash=hash_password(password),
                    totp_secret_ref=ref,
                    created_at=self._clock(),
                )
            )
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="break_glass.admin_created",
                object_ref=f"local_admin:{username}",
                request_id=request_id,
            )
        return Enrollment(username, password, totp_secret, otpauth_uri(username, totp_secret))

    async def confirm_admin(
        self, principal: Principal, totp: str, *, request_id: str | None = None
    ) -> BreakGlassStatus:
        """Confirm enrolment with a valid code from the authenticator.

        Raises:
            NotFoundError: If no administrator is enrolled.
            FieldErrors: If the code is wrong.
        """
        require(principal, "manage", "break_glass")
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            admin = (
                (await session.execute(select(LocalAdminRow).with_for_update())).scalars().first()
            )
            if admin is None:
                raise NotFoundError("no break-glass administrator is enrolled")
            secret = ""
            if self._reader is not None:
                try:
                    secret = await self._reader.get(admin.totp_secret_ref)
                except LookupError:
                    secret = ""
            ok = bool(secret) and verify_totp(secret, totp, now.timestamp())
            if ok:
                admin.totp_confirmed_at = now
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="break_glass.admin_confirmed" if ok else "break_glass.confirm_failed",
                object_ref=f"local_admin:{admin.username}",
                request_id=request_id,
            )
            await session.flush()
            status = await self._status(session)
        if not ok:  # raised after the audit event committed
            errors = FieldErrors()
            errors.add("totp", "invalid", "that code is not valid; check the authenticator")
            raise errors
        return status

    async def delete_admin(self, principal: Principal, *, request_id: str | None = None) -> None:
        """Remove the administrator and switch break-glass login off."""
        require(principal, "manage", "break_glass")
        async with admin_session(self._db.admin) as session:
            removed = (await session.execute(delete(LocalAdminRow))).rowcount  # type: ignore[attr-defined]
            if not removed:
                raise NotFoundError("no break-glass administrator is enrolled")
            await self._store(session, None, None, None, None)
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="break_glass.admin_deleted",
                request_id=request_id,
            )

    @staticmethod
    async def _store(
        session: object,
        until: datetime | None,
        by: str | None,
        at: datetime | None,
        reason: str | None,
    ) -> None:
        setting = await session.get(BreakGlassSettingRow, 1)  # type: ignore[attr-defined]
        if setting is None:
            setting = BreakGlassSettingRow(id=1)
            session.add(setting)  # type: ignore[attr-defined]
        setting.enabled_until, setting.enabled_by = until, by
        setting.enabled_at, setting.reason = at, reason

    async def _status(self, session: object) -> BreakGlassStatus:
        now = self._clock()
        setting = await session.get(BreakGlassSettingRow, 1)  # type: ignore[attr-defined]
        active = bool(
            setting is not None
            and setting.enabled_until is not None
            and setting.enabled_until > now
        )
        admin_row = (
            (
                await session.execute(select(LocalAdminRow).order_by(LocalAdminRow.created_at))  # type: ignore[attr-defined]
            )
            .scalars()
            .first()
        )
        admin = (
            BreakGlassAdmin(
                admin_row.username,
                admin_row.totp_confirmed_at is not None,
                admin_row.created_at,
                admin_row.last_login_at,
                admin_row.locked_until
                if admin_row.locked_until and admin_row.locked_until > now
                else None,
            )
            if admin_row is not None
            else None
        )
        return BreakGlassStatus(
            forced_by_deployment=self._forced,
            enabled=self._forced or active,
            enabled_until=setting.enabled_until if active and setting else None,
            enabled_by=setting.enabled_by if active and setting else None,
            reason=setting.reason if active and setting else None,
            admin=admin,
        )


__all__ = [
    "BreakGlassAdmin",
    "BreakGlassService",
    "BreakGlassStatus",
    "Enrollment",
    "otpauth_uri",
]
