"""Browser login: identity providers, sessions, role mapping and the break-glass administrator.

After a successful OIDC login (or break-glass login) the controller issues its own session and the
rest of the backend never sees which provider was used. A session is a random cookie value stored
only as SHA-256, with an absolute lifetime and an idle timeout. Its roles are decided once, at
login, from the provider's group claim through ``idp_role_mapping``; the result is the same
:class:`~switchyard.services.context.Principal` model that API tokens use.
"""

import base64
import hashlib
import hmac
import secrets
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select, update

from switchyard.adapters.secrets import SecretsReader
from switchyard.db import audit_log
from switchyard.db.auth_models import (
    IdentityProviderRow,
    IdpRoleMappingRow,
    LocalAdminRow,
    LoginStateRow,
    UserSessionRow,
)
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.ids import uuid7
from switchyard.services.changesets import ROLE_RANK
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.oidc import OidcClient, OidcError, ProviderConfig, new_pkce
from switchyard.services.tokens import hash_token

SESSION_TTL = timedelta(hours=8)
IDLE_TTL = timedelta(hours=1)
LOGIN_TTL = timedelta(minutes=10)
LOCKOUT_AFTER = 5
LOCKOUT_FOR = timedelta(minutes=15)
SESSION_PREFIX = "sysess_"


class LoginError(Exception):
    """Login was refused. The message is safe to show to the user."""


@dataclass(frozen=True)
class ProviderInfo:
    """What the login page shows for one provider."""

    id: UUID
    name: str
    protocol: str


@dataclass(frozen=True)
class SessionInfo:
    """A valid session and who it belongs to."""

    id: UUID
    subject: str
    name: str
    email: str | None
    grants: tuple[tuple[UUID | None, Role], ...]
    csrf_token: str
    break_glass: bool
    expires_at: datetime

    @property
    def is_system(self) -> bool:
        """True if the user holds the system-admin role."""
        return any(role is Role.SYSTEM_ADMIN for _, role in self.grants)

    def principal(self) -> Principal:
        """Build the principal services use."""
        if self.is_system:
            return Principal(f"user:{self.subject}", Role.SYSTEM_ADMIN)
        per_tenant: dict[UUID, Role] = {}
        for tenant_id, role in self.grants:
            if tenant_id is None:
                continue
            current = per_tenant.get(tenant_id)
            if current is None or ROLE_RANK[role] > ROLE_RANK[current]:
                per_tenant[tenant_id] = role
        highest = max(per_tenant.values(), key=lambda r: ROLE_RANK[r], default=Role.VIEWER)
        return Principal(
            f"user:{self.subject}",
            highest,
            frozenset(per_tenant),
            tuple(sorted(per_tenant.items(), key=lambda kv: str(kv[0]))),
        )


@dataclass(frozen=True)
class LoginResult:
    """A fresh session: the cookie value (shown once) and where to send the browser."""

    cookie: str
    session: SessionInfo
    return_to: str


def safe_return_to(value: str | None) -> str:
    """Accept only same-site absolute paths, so login cannot be used as an open redirect.

    Example:
        >>> safe_return_to("/t/acme/virtual-services")
        '/t/acme/virtual-services'
        >>> safe_return_to("https://evil.example") , safe_return_to("//evil.example")
        ('/', '/')
    """
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def hash_password(password: str, *, n: int = 2**15) -> str:
    """Hash a password with scrypt. Format: ``scrypt$n$r$p$salt$hash`` (base64)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=8, p=1, dklen=32, maxmem=2**27)
    return "$".join(
        [
            "scrypt",
            str(n),
            "8",
            "1",
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Check a password against :func:`hash_password` output in constant time."""
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
            maxmem=2**27,
        )
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except (ValueError, TypeError):
        return False


def totp_code(secret_b32: str, at: float, *, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 time-based one-time password (HMAC-SHA1).

    Example:
        >>> totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 59, digits=8)
        '94287082'
    """
    key = base64.b32decode(secret_b32.replace(" ", "").upper() + "=" * (-len(secret_b32) % 8))
    mac = hmac.new(key, struct.pack(">Q", int(at // step)), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = (struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**digits
    return str(value).zfill(digits)


def verify_totp(secret_b32: str, code: str, at: float, *, window: int = 1) -> bool:
    """Accept the current code and ``window`` steps either side (clock drift)."""
    return any(
        hmac.compare_digest(totp_code(secret_b32, at + offset * 30), code.strip())
        for offset in range(-window, window + 1)
    )


class SessionService:
    """Login, session lookup and logout."""

    def __init__(
        self,
        db: Databases,
        oidc: OidcClient,
        secrets_reader: SecretsReader | None = None,
        *,
        break_glass_enabled: bool = False,
        clock: Clock = utcnow,
    ) -> None:
        """Create the service.

        Args:
            db: Database handles (login uses the admin role: no tenant is known yet).
            oidc: OpenID Connect client.
            secrets_reader: Resolves provider client secrets and TOTP secrets.
            break_glass_enabled: Whether the local administrator may log in (off by default).
            clock: Time source.
        """
        self._db = db
        self._oidc = oidc
        self._secrets = secrets_reader
        self._break_glass = break_glass_enabled
        self._clock = clock

    @property
    def break_glass_enabled(self) -> bool:
        """Whether the break-glass login is switched on."""
        return self._break_glass

    # -- providers --------------------------------------------------------------------------------

    async def providers(self) -> list[ProviderInfo]:
        """Enabled providers, for the login page."""
        async with admin_session(self._db.admin) as session:
            rows = (
                await session.execute(
                    select(IdentityProviderRow)
                    .where(IdentityProviderRow.enabled.is_(True))
                    .order_by(IdentityProviderRow.name)
                )
            ).scalars()
            return [ProviderInfo(r.id, r.name, r.protocol) for r in rows]

    async def _provider(
        self, session: Any, provider: str
    ) -> tuple[IdentityProviderRow, ProviderConfig]:
        try:
            wanted = UUID(provider)
            query = select(IdentityProviderRow).where(IdentityProviderRow.id == wanted)
        except ValueError:
            query = select(IdentityProviderRow).where(IdentityProviderRow.name == provider)
        row = (await session.execute(query)).scalar_one_or_none()
        if row is None or not row.enabled:
            raise LoginError("unknown identity provider")
        if row.protocol != "oidc":
            raise LoginError("this provider's protocol is not supported yet")
        client_secret: str | None = None
        if row.client_secret_ref:
            if self._secrets is None:
                raise LoginError("the controller has no secrets backend for client secrets")
            try:
                client_secret = await self._secrets.get(row.client_secret_ref)
            except LookupError as exc:
                raise LoginError("the provider's client secret is not available") from exc
        return row, ProviderConfig(
            id=str(row.id),
            name=row.name,
            issuer_url=row.issuer_url,
            client_id=row.client_id,
            client_secret=client_secret,
            scopes=row.scopes,
            username_claim=row.username_claim,
            email_claim=row.email_claim,
            groups_claim=row.groups_claim,
        )

    # -- OIDC login -------------------------------------------------------------------------------

    async def begin_login(self, provider: str, *, redirect_uri: str, return_to: str | None) -> str:
        """Start a login; returns the URL to redirect the browser to."""
        async with admin_session(self._db.admin) as session:
            row, cfg = await self._provider(session, provider)
            verifier, challenge = new_pkce()
            state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            await session.execute(
                delete(LoginStateRow).where(LoginStateRow.expires_at < self._clock())
            )
            session.add(
                LoginStateRow(
                    state=state,
                    provider_id=row.id,
                    nonce=nonce,
                    code_verifier=verifier,
                    return_to=safe_return_to(return_to),
                    expires_at=self._clock() + LOGIN_TTL,
                )
            )
        try:
            return await self._oidc.authorization_url(
                cfg, redirect_uri=redirect_uri, state=state, nonce=nonce, code_challenge=challenge
            )
        except OidcError as exc:
            raise LoginError(str(exc)) from exc

    async def complete_login(self, *, state: str, code: str, redirect_uri: str) -> LoginResult:
        """Finish a login from the provider's redirect.

        Raises:
            LoginError: If the state is unknown, expired or reused, the provider refuses, or the
                user's groups grant no access. The refusal is recorded (and the state consumed)
                before the error is raised.
        """
        outcome = await self._complete_login(state, code, redirect_uri)
        if isinstance(outcome, str):
            raise LoginError(outcome)
        return outcome

    async def _complete_login(self, state: str, code: str, redirect_uri: str) -> LoginResult | str:
        async with admin_session(self._db.admin) as session:
            pending = (
                await session.execute(
                    delete(LoginStateRow)
                    .where(LoginStateRow.state == state)
                    .returning(LoginStateRow)
                )
            ).scalar_one_or_none()
            if pending is None or pending.expires_at <= self._clock():
                return "this login attempt has expired; start again"
            row, cfg = await self._provider(session, str(pending.provider_id))
            try:
                claims = await self._oidc.exchange(
                    cfg,
                    code=code,
                    redirect_uri=redirect_uri,
                    code_verifier=pending.code_verifier,
                    nonce=pending.nonce,
                )
            except OidcError as exc:
                await audit_log.record(
                    session,
                    tenant_id=None,
                    actor=f"login:{cfg.name}",
                    action="auth.login_failed",
                    diff={"reason": str(exc)},
                )
                return str(exc)
            groups = claims.get(cfg.groups_claim, [])
            groups = [groups] if isinstance(groups, str) else [str(g) for g in groups]
            mappings = (
                await session.execute(
                    select(IdpRoleMappingRow).where(
                        IdpRoleMappingRow.provider_id == row.id,
                        IdpRoleMappingRow.group_name.in_(groups or [""]),
                    )
                )
            ).scalars()
            grants = tuple((m.scope_tenant_id, Role(m.role)) for m in mappings)
            subject = f"{cfg.name}:{claims['sub']}"
            name = str(claims.get(cfg.username_claim) or claims["sub"])
            email = claims.get(cfg.email_claim)
            if not grants:
                await audit_log.record(
                    session,
                    tenant_id=None,
                    actor=f"user:{subject}",
                    action="auth.login_denied",
                    diff={"reason": "no group grants access"},
                )
                return "your account is not a member of any group with access"
            result = await self._create_session(
                session,
                subject=subject,
                provider_id=row.id,
                name=name,
                email=str(email) if email else None,
                grants=grants,
                break_glass=False,
                return_to=pending.return_to,
            )
            await audit_log.record(
                session,
                tenant_id=None,
                actor=f"user:{subject}",
                action="auth.login",
                diff={"provider": cfg.name, "grants": _grants_json(grants)},
            )
            return result

    async def _create_session(
        self,
        session: Any,
        *,
        subject: str,
        provider_id: UUID | None,
        name: str,
        email: str | None,
        grants: tuple[tuple[UUID | None, Role], ...],
        break_glass: bool,
        return_to: str,
    ) -> LoginResult:
        cookie = SESSION_PREFIX + secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = self._clock()
        row = UserSessionRow(
            id=uuid7(),
            token_hash=hash_token(cookie),
            csrf_token=csrf,
            subject=subject,
            provider_id=provider_id,
            name=name,
            email=email,
            grants=_grants_json(grants),
            break_glass=break_glass,
            created_at=now,
            expires_at=now + SESSION_TTL,
            last_seen_at=now,
        )
        session.add(row)
        info = _info(row)
        return LoginResult(cookie, info, return_to)

    # -- sessions ---------------------------------------------------------------------------------

    async def authenticate(self, cookie: str) -> SessionInfo | None:
        """Return the session for a cookie value, or ``None`` (unknown, expired, idle, revoked)."""
        if not cookie.startswith(SESSION_PREFIX):
            return None
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(UserSessionRow).where(UserSessionRow.token_hash == hash_token(cookie))
                )
            ).scalar_one_or_none()
            if (
                row is None
                or row.revoked_at is not None
                or row.expires_at <= now
                or now - row.last_seen_at > IDLE_TTL
            ):
                return None
            if now - row.last_seen_at > timedelta(seconds=30):
                await session.execute(
                    update(UserSessionRow)
                    .where(UserSessionRow.id == row.id)
                    .values(last_seen_at=now)
                )
            return _info(row)

    async def logout(self, cookie: str) -> None:
        """Revoke a session (idempotent) and audit it."""
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(UserSessionRow).where(UserSessionRow.token_hash == hash_token(cookie))
                )
            ).scalar_one_or_none()
            if row is None or row.revoked_at is not None:
                return
            row.revoked_at = self._clock()
            await audit_log.record(
                session,
                tenant_id=None,
                actor=f"user:{row.subject}",
                action="auth.logout",
            )

    # -- break-glass ------------------------------------------------------------------------------

    async def create_local_admin(self, username: str, password: str, totp_secret_ref: str) -> None:
        """Create or replace the break-glass administrator (an operator command, not an API)."""
        if len(password) < 16:
            raise ValueError("the break-glass password must be at least 16 characters")
        async with admin_session(self._db.admin) as session:
            await session.execute(delete(LocalAdminRow).where(LocalAdminRow.username == username))
            session.add(
                LocalAdminRow(
                    username=username,
                    password_hash=hash_password(password),
                    totp_secret_ref=totp_secret_ref,
                )
            )
            await audit_log.record(
                session,
                tenant_id=None,
                actor="system:cli",
                action="auth.local_admin_created",
                object_ref=f"local_admin:{username}",
            )

    async def break_glass_login(self, username: str, password: str, code: str) -> LoginResult:
        """Log the local administrator in. Every attempt is audited; failures lock the account."""
        if not self._break_glass:
            raise LoginError("break-glass login is not enabled")
        outcome = await self._break_glass_login(username, password, code)
        if isinstance(outcome, str):
            raise LoginError(outcome)
        return outcome

    async def _break_glass_login(
        self, username: str, password: str, code: str
    ) -> LoginResult | str:
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            admin = (
                await session.execute(
                    select(LocalAdminRow)
                    .where(LocalAdminRow.username == username)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if admin is not None and admin.locked_until and admin.locked_until > now:
                return "too many failed attempts; try again later"
            ok = False
            if admin is not None and verify_password(password, admin.password_hash):
                try:
                    secret = await self._secrets.get(admin.totp_secret_ref) if self._secrets else ""
                except LookupError:
                    secret = ""
                ok = bool(secret) and verify_totp(secret, code, now.timestamp())
            elif admin is None:
                verify_password(password, hash_password("x" * 16, n=2**10))  # keep timing similar
            if not ok:
                if admin is not None:
                    admin.failed_attempts += 1
                    if admin.failed_attempts >= LOCKOUT_AFTER:
                        admin.locked_until = now + LOCKOUT_FOR
                        admin.failed_attempts = 0
                await audit_log.record(
                    session,
                    tenant_id=None,
                    actor=f"local:{username}",
                    action="auth.break_glass_failed",
                )
                return "invalid credentials"
            assert admin is not None  # noqa: S101
            admin.failed_attempts = 0
            result = await self._create_session(
                session,
                subject=f"local:{username}",
                provider_id=None,
                name=username,
                email=None,
                grants=((None, Role.SYSTEM_ADMIN),),
                break_glass=True,
                return_to="/",
            )
            await audit_log.record(
                session,
                tenant_id=None,
                actor=f"local:{username}",
                action="auth.break_glass_login",
            )
            return result


def _grants_json(grants: tuple[tuple[UUID | None, Role], ...]) -> list[dict[str, Any]]:
    return [
        {"tenant_id": str(tenant_id) if tenant_id else None, "role": role.value}
        for tenant_id, role in grants
    ]


def _info(row: UserSessionRow) -> SessionInfo:
    return SessionInfo(
        id=row.id,
        subject=row.subject,
        name=row.name,
        email=row.email,
        grants=tuple(
            (UUID(g["tenant_id"]) if g["tenant_id"] else None, Role(g["role"])) for g in row.grants
        ),
        csrf_token=row.csrf_token,
        break_glass=row.break_glass,
        expires_at=row.expires_at,
    )


__all__ = [
    "LoginError",
    "LoginResult",
    "ProviderInfo",
    "SessionInfo",
    "SessionService",
    "hash_password",
    "safe_return_to",
    "totp_code",
    "verify_password",
    "verify_totp",
]
