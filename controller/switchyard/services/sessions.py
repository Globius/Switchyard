"""Browser login: identity providers, sessions, role mapping and the break-glass administrator.

After a successful OIDC login (or break-glass login) the controller issues its own session and the
rest of the backend never sees which provider was used. A session is a random cookie value stored
only as SHA-256, with an absolute lifetime and an idle timeout. Its roles are decided at
login, from the provider's group claim through ``idp_role_mapping``, and recomputed from the stored
groups and the *current* mappings when older than a minute (ADR 0030); the result is the same
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

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from switchyard.adapters.secrets import SecretsReader
from switchyard.db import audit_log
from switchyard.db.auth_models import (
    BreakGlassSettingRow,
    IdentityProviderRow,
    IdpRoleMappingRow,
    IdpTestRow,
    LocalAdminRow,
    LoginStateRow,
    SamlAssertionSeenRow,
    UserAccountRow,
    UserSessionRow,
)
from switchyard.db.models import TenantRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.ids import uuid7
from switchyard.services import saml
from switchyard.services.changesets import ROLE_RANK
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.identity_providers import clean_claims
from switchyard.services.oidc import OidcClient, OidcError, ProviderConfig, new_pkce
from switchyard.services.throttle import LoginThrottle, account_rule, source_rule
from switchyard.services.tokens import hash_token

SESSION_TTL = timedelta(hours=4)
IDLE_TTL = timedelta(minutes=30)
BREAK_GLASS_TTL = timedelta(hours=1)
BREAK_GLASS_IDLE = timedelta(minutes=15)
#: Grants are recomputed from the stored groups when older than this (and on ``/auth/refresh``).
GRANTS_MAX_AGE = timedelta(seconds=60)
#: ``user_account.last_seen_at`` is written at most this often.
USER_SEEN_EVERY = timedelta(minutes=1)
MAX_STORED_GROUPS = 200
LOGIN_TTL = timedelta(minutes=10)
SESSION_PREFIX = "sysess_"


EXPIRED_MESSAGE = "this login attempt has expired; start again"
NO_ACCESS_MESSAGE = "your account is not a member of any group with access"
DISABLED_MESSAGE = "this account has been disabled by an administrator"


def login_error_code(message: str) -> str:
    """The ``/login?error=<code>`` for a refusal message (``provider_error`` when unknown)."""
    return {
        EXPIRED_MESSAGE: "expired",
        NO_ACCESS_MESSAGE: "no_access",
        DISABLED_MESSAGE: "disabled",
    }.get(message, "provider_error")


class UserDisabledError(Exception):
    """The session belongs to a user an administrator disabled; the session was revoked."""


class LoginError(Exception):
    """Login was refused. The message is safe to show to the user.

    ``code`` is the short reason a browser login redirects with (``/login?error=<code>``):
    ``provider_error``, ``expired``, ``no_access`` or ``disabled``. When not given it is derived
    from the message.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        """Create the error."""
        super().__init__(message)
        self.code = code or login_error_code(message)


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
    idle_timeout: timedelta = IDLE_TTL
    grants_computed_at: datetime | None = None
    user_id: UUID | None = None
    provider_id: UUID | None = None
    groups: tuple[str, ...] = ()

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
class GrantVia:
    """One grant and the mapping that produced it (``tenant_id`` is ``None`` for system-admin)."""

    tenant_id: UUID | None
    role: Role
    provider: str
    group: str


@dataclass(frozen=True)
class TestDone:
    """A test sign-in finished: no session was created; send the browser to ``return_to``."""

    return_to: str


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
    if (
        value
        and value.startswith("/")
        and not value.startswith("//")
        and "\\" not in value
        and value.isprintable()  # browsers drop tabs and newlines inside URLs: "/\t/host"
    ):
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


def matching_totp_step(secret_b32: str, code: str, at: float, *, window: int = 1) -> int | None:
    """The time step (unix time // 30) whose code equals ``code``, within ``window`` steps.

    Example:
        >>> secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        >>> matching_totp_step(secret, totp_code(secret, 90), 90)
        3
        >>> matching_totp_step(secret, "000000", 90) is None
        True
    """
    found: int | None = None
    for offset in range(-window, window + 1):  # no early exit: constant work per attempt
        if hmac.compare_digest(totp_code(secret_b32, at + offset * 30), code.strip()):
            found = int((at + offset * 30) // 30)
    return found


def verify_totp(secret_b32: str, code: str, at: float, *, window: int = 1) -> bool:
    """Accept the current code and ``window`` steps either side (clock drift)."""
    return matching_totp_step(secret_b32, code, at, window=window) is not None


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
        self.throttle = LoginThrottle(db, clock)

    @property
    def oidc(self) -> OidcClient:
        """The OpenID Connect client (shared with identity-provider administration)."""
        return self._oidc

    @property
    def break_glass_enabled(self) -> bool:
        """Whether the deployment forces the break-glass login on (``SWITCHYARD_BREAK_GLASS=1``)."""
        return self._break_glass

    async def break_glass_on(self) -> bool:
        """True if break-glass login is on: forced, or switched on and not yet expired."""
        if self._break_glass:
            return True
        async with admin_session(self._db.admin) as session:
            setting = await session.get(BreakGlassSettingRow, 1)
            return bool(
                setting is not None
                and setting.enabled_until is not None
                and setting.enabled_until > self._clock()
            )

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

    async def _provider_row(
        self, session: Any, provider: str, *, allow_disabled: bool = False
    ) -> IdentityProviderRow:
        try:
            wanted = UUID(provider)
            query = select(IdentityProviderRow).where(IdentityProviderRow.id == wanted)
        except ValueError:
            query = select(IdentityProviderRow).where(IdentityProviderRow.name == provider)
        row = (await session.execute(query)).scalar_one_or_none()
        if row is None or not (row.enabled or allow_disabled):
            raise LoginError("unknown identity provider")
        return row  # type: ignore[no-any-return]

    async def _provider(
        self, session: Any, provider: str, *, allow_disabled: bool = False
    ) -> tuple[IdentityProviderRow, ProviderConfig]:
        row = await self._provider_row(session, provider, allow_disabled=allow_disabled)
        if row.protocol != "oidc" or row.issuer_url is None or row.client_id is None:
            raise LoginError("this provider does not use OpenID Connect")
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

    async def begin_login(
        self,
        provider: str,
        *,
        redirect_uri: str,
        return_to: str | None,
        test_id: UUID | None = None,
    ) -> str:
        """Start a login; returns the URL to redirect the browser to (OIDC or SAML).

        With ``test_id`` this is an administrator's test sign-in: the callback records the outcome
        under that id and creates no session. The provider may be disabled for a test.
        """
        purpose = "test" if test_id else "login"
        async with admin_session(self._db.admin) as session:
            row = await self._provider_row(session, provider, allow_disabled=test_id is not None)
            if row.protocol == "saml":
                return await self._begin_saml(session, row, redirect_uri, return_to, test_id)
            row, cfg = await self._provider(session, provider, allow_disabled=test_id is not None)
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
                    purpose=purpose,
                    test_id=test_id,
                    expires_at=self._clock() + LOGIN_TTL,
                )
            )
        try:
            return await self._oidc.authorization_url(
                cfg, redirect_uri=redirect_uri, state=state, nonce=nonce, code_challenge=challenge
            )
        except OidcError as exc:
            raise LoginError(str(exc)) from exc

    async def complete_login(
        self, *, state: str, code: str, redirect_uri: str
    ) -> LoginResult | TestDone:
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

    async def _complete_login(
        self, state: str, code: str, redirect_uri: str
    ) -> LoginResult | TestDone | str:
        async with admin_session(self._db.admin) as session:
            pending = (
                await session.execute(
                    delete(LoginStateRow)
                    .where(LoginStateRow.state == state)
                    .returning(LoginStateRow)
                )
            ).scalar_one_or_none()
            if pending is None or pending.expires_at <= self._clock():
                return EXPIRED_MESSAGE
            is_test = pending.purpose == "test"
            row, cfg = await self._provider(
                session, str(pending.provider_id), allow_disabled=is_test
            )
            try:
                claims = await self._oidc.exchange(
                    cfg,
                    code=code,
                    redirect_uri=redirect_uri,
                    code_verifier=pending.code_verifier or "",
                    nonce=pending.nonce or "",
                )
            except OidcError as exc:
                if is_test:
                    return await self._finish_test(session, pending, row, error=str(exc))
                await audit_log.record(
                    session,
                    tenant_id=None,
                    actor=f"login:{cfg.name}",
                    action="auth.login_failed",
                    diff={"reason": str(exc)},
                )
                return str(exc)
            raw_groups = claims.get(cfg.groups_claim)
            groups = (
                [raw_groups] if isinstance(raw_groups, str) else [str(g) for g in raw_groups or []]
            )
            if is_test:
                return await self._finish_test(
                    session,
                    pending,
                    row,
                    subject=str(claims["sub"]),
                    username=str(claims.get(cfg.username_claim) or ""),
                    email=str(claims[cfg.email_claim]) if claims.get(cfg.email_claim) else None,
                    groups=groups,
                    claims=claims,
                    groups_claim=cfg.groups_claim,
                    username_claim=cfg.username_claim,
                )
            return await self._grant_login(
                session,
                row=row,
                provider_name=cfg.name,
                subject_id=str(claims["sub"]),
                name=str(claims.get(cfg.username_claim) or claims["sub"]),
                email=claims.get(cfg.email_claim),
                groups=groups,
                return_to=pending.return_to,
            )

    async def _grant_login(
        self,
        session: Any,
        *,
        row: IdentityProviderRow,
        provider_name: str,
        subject_id: str,
        name: str,
        email: Any,
        groups: list[str],
        return_to: str,
    ) -> LoginResult | str:
        """Map groups to grants and create the session (shared by OIDC and SAML)."""
        grants = _distinct(await self.compute_grants(session, row.id, groups))
        subject = f"{provider_name}:{subject_id}"
        user = await self._upsert_user(
            session,
            provider_id=row.id,
            subject=subject_id,
            username=name,
            email=str(email) if email else None,
            groups=groups,
        )
        if user.disabled_at is not None:
            await audit_log.record(
                session,
                tenant_id=None,
                actor=f"user:{subject}",
                action="auth.login_denied",
                diff={"reason": "user disabled"},
            )
            return DISABLED_MESSAGE
        if not grants:
            await audit_log.record(
                session,
                tenant_id=None,
                actor=f"user:{subject}",
                action="auth.login_denied",
                diff={"reason": "no group grants access"},
            )
            return NO_ACCESS_MESSAGE
        result = await self._create_session(
            session,
            subject=subject,
            provider_id=row.id,
            name=name,
            email=str(email) if email else None,
            grants=grants,
            break_glass=False,
            return_to=return_to,
            user_id=user.id,
            groups=groups,
        )
        await audit_log.record(
            session,
            tenant_id=None,
            actor=f"user:{subject}",
            action="auth.login",
            diff={"provider": provider_name, "grants": _grants_json(grants)},
        )
        return result

    async def compute_grants(
        self, session: Any, provider_id: UUID | None, groups: list[str] | tuple[str, ...]
    ) -> list[GrantVia]:
        """Grants for ``groups`` of one provider under the *current* mappings, with their source."""
        if provider_id is None or not groups:
            return []
        rows = (
            await session.execute(
                select(IdpRoleMappingRow, IdentityProviderRow.name)
                .join(IdentityProviderRow, IdentityProviderRow.id == IdpRoleMappingRow.provider_id)
                .where(
                    IdpRoleMappingRow.provider_id == provider_id,
                    IdpRoleMappingRow.group_name.in_(list(groups)),
                )
                .order_by(IdpRoleMappingRow.group_name, IdpRoleMappingRow.id)
            )
        ).all()
        return [GrantVia(m.scope_tenant_id, Role(m.role), name, m.group_name) for m, name in rows]

    async def _upsert_user(
        self,
        session: Any,
        *,
        provider_id: UUID,
        subject: str,
        username: str,
        email: str | None,
        groups: list[str],
    ) -> UserAccountRow:
        """Create or update the ``user_account`` of someone who just signed in."""
        now = self._clock()
        stored = list(dict.fromkeys(groups))[:MAX_STORED_GROUPS]
        values = {
            "username": username,
            "email": email,
            "groups": stored,
            "last_login_at": now,
            "last_seen_at": now,
        }
        stmt = (
            pg_insert(UserAccountRow)
            .values(
                id=uuid7(),
                provider_id=provider_id,
                subject=subject,
                first_login_at=now,
                **values,
            )
            .on_conflict_do_update(constraint="uq_user_account_provider_subject", set_=values)
            .returning(UserAccountRow)
        )
        return (await session.execute(stmt)).scalar_one()  # type: ignore[no-any-return]

    async def _finish_test(
        self,
        session: Any,
        pending: LoginStateRow,
        row: IdentityProviderRow,
        *,
        error: str | None = None,
        subject: str = "",
        username: str = "",
        email: str | None = None,
        groups: list[str] | None = None,
        claims: dict[str, Any] | None = None,
        groups_claim: str = "groups",
        username_claim: str = "username",
    ) -> TestDone:
        """Record what a test sign-in learned. No session and no user account are created."""
        groups = groups or []
        warnings: list[str] = []
        would_grant: list[dict[str, Any]] = []
        if error is None:
            if not groups:
                warnings.append(f"the groups claim {groups_claim!r} is missing or empty")
            if not username:
                warnings.append(f"the username claim {username_claim!r} is missing")
            if email is None:
                warnings.append("the e-mail claim is missing")
            granted = await self.compute_grants(session, row.id, groups)
            if groups and not granted:
                warnings.append(
                    "no role mapping matches these groups; this person would get no access"
                )
            names = {t.id: t.name for t in (await session.execute(select(TenantRow))).scalars()}
            would_grant = [
                {
                    "tenant": names.get(g.tenant_id) if g.tenant_id else None,
                    "role": g.role.value,
                    "via": {"provider": g.provider, "group": g.group},
                }
                for g in granted
            ]
        result = {
            "subject": subject,
            "username": username,
            "email": email,
            "groups": groups,
            "claims": clean_claims(claims or {}),
            "would_grant": would_grant,
            "warnings": warnings,
            "error": error,
        }
        if pending.test_id is not None:
            test = await session.get(IdpTestRow, pending.test_id)
            if test is not None and test.status == "pending":
                test.status, test.result = "done", result
        await audit_log.record(
            session,
            tenant_id=None,
            actor=f"login:{row.name}",
            action="idp.test_done",
            object_ref=f"idp:{row.name}",
            diff={"ok": error is None},
        )
        return TestDone(pending.return_to)

    # -- SAML login -------------------------------------------------------------------------------

    async def _saml_config(
        self, row: IdentityProviderRow, public_url: str, *, with_key: bool = False
    ) -> saml.SamlConfig:
        s = row.saml or {}
        key_pem = ""
        if with_key and s.get("sign_requests") and s.get("sp_signing_key_ref"):
            if self._secrets is None:
                raise LoginError(
                    "the controller has no secrets backend for the request signing key"
                )
            try:
                key_pem = await self._secrets.get(str(s["sp_signing_key_ref"]))
            except LookupError as exc:
                raise LoginError("the request signing key is not available") from exc
        return saml.SamlConfig(
            id=str(row.id),
            name=row.name,
            idp_entity_id=str(s.get("idp_entity_id", "")),
            sso_url=str(s.get("sso_url", "")),
            signing_certificates=tuple(str(c) for c in s.get("signing_certificates", [])),
            sp_entity_id=str(
                s.get("sp_entity_id") or f"{public_url.rstrip('/')}/api/v1/auth/saml/{row.id}"
            ),
            name_id_format=str(s.get("name_id_format") or saml.DEFAULT_NAME_ID_FORMAT),
            attribute_username=str(s.get("attribute_username", "username")),
            attribute_email=str(s.get("attribute_email", "email")),
            attribute_groups=str(s.get("attribute_groups", "groups")),
            sign_requests=bool(s.get("sign_requests", False)),
            sp_signing_certificate=str(s.get("sp_signing_certificate", "")),
            sp_signing_key_pem=key_pem,
        )

    async def saml_metadata(self, provider: str, *, acs_url: str) -> str:
        """The SP metadata XML of a SAML provider (public; an IdP may fetch it by URL).

        Raises:
            LoginError: If the provider is unknown, disabled or not a SAML provider.
        """
        async with admin_session(self._db.admin) as session:
            row = await self._provider_row(session, provider)
        if row.protocol != "saml":
            raise LoginError("this provider does not use SAML")
        acs_url = _canonical_acs(acs_url, row.id)
        cfg = await self._saml_config(row, _base_of(acs_url))
        return saml.sp_metadata(cfg, acs_url=acs_url)

    async def _begin_saml(
        self,
        session: Any,
        row: IdentityProviderRow,
        acs_url: str,
        return_to: str | None,
        test_id: UUID | None = None,
    ) -> str:
        acs_url = _canonical_acs(acs_url, row.id)
        cfg = await self._saml_config(row, _base_of(acs_url), with_key=True)
        state, request_id = secrets.token_urlsafe(32), saml.new_request_id()
        await session.execute(delete(LoginStateRow).where(LoginStateRow.expires_at < self._clock()))
        session.add(
            LoginStateRow(
                state=state,
                provider_id=row.id,
                request_id=request_id,
                return_to=safe_return_to(return_to),
                purpose="test" if test_id else "login",
                test_id=test_id,
                expires_at=self._clock() + LOGIN_TTL,
            )
        )
        return saml.authn_request_url(
            cfg, acs_url=acs_url, request_id=request_id, relay_state=state, now=self._clock()
        )

    async def complete_saml_login(
        self, *, provider: str, saml_response: str, relay_state: str, acs_url: str
    ) -> LoginResult | TestDone:
        """Finish a SAML login from the assertion consumer service POST.

        The login state (found by ``relay_state``) is single use and names the request id the
        response must answer; IdP-initiated responses have no state and are refused. Every refusal
        is audited as ``auth.login_failed``.

        Raises:
            LoginError: For any refusal; the message is safe to show.
        """
        outcome = await self._complete_saml(provider, saml_response, relay_state, acs_url)
        if isinstance(outcome, str):
            raise LoginError(outcome)
        return outcome

    async def _complete_saml(
        self, provider: str, saml_response: str, relay_state: str, acs_url: str
    ) -> LoginResult | TestDone | str:
        async with admin_session(self._db.admin) as session:
            row = await self._provider_row(session, provider, allow_disabled=True)
            if row.protocol != "saml":
                return "this provider does not use SAML"
            pending = (
                await session.execute(
                    delete(LoginStateRow)
                    .where(LoginStateRow.state == relay_state, LoginStateRow.provider_id == row.id)
                    .returning(LoginStateRow)
                )
            ).scalar_one_or_none()
            if pending is None or pending.expires_at <= self._clock() or not pending.request_id:
                await self._login_failed(session, row.name, "no login in progress (or expired)")
                return EXPIRED_MESSAGE
            is_test = pending.purpose == "test"
            if not row.enabled and not is_test:
                return "unknown identity provider"
            acs_url = _canonical_acs(acs_url, row.id)
            cfg = await self._saml_config(row, _base_of(acs_url))
            now = self._clock()
            try:
                assertion = saml.verify_response(
                    cfg, saml_response, acs_url=acs_url, request_id=pending.request_id, now=now
                )
            except saml.SamlError as exc:
                if is_test:
                    return await self._finish_test(session, pending, row, error=str(exc))
                await self._login_failed(session, row.name, str(exc))
                return str(exc)
            await session.execute(
                delete(SamlAssertionSeenRow).where(SamlAssertionSeenRow.expires_at < now)
            )
            inserted = (
                await session.execute(
                    pg_insert(SamlAssertionSeenRow)
                    .values(
                        provider_id=row.id,
                        assertion_id=assertion.assertion_id,
                        expires_at=assertion.not_on_or_after,
                    )
                    .on_conflict_do_nothing()
                    .returning(SamlAssertionSeenRow.assertion_id)
                )
            ).first()
            if inserted is None:
                await self._login_failed(session, row.name, "assertion replayed")
                return "this sign-in response was already used"
            if is_test:
                return await self._finish_test(
                    session,
                    pending,
                    row,
                    subject=assertion.subject,
                    username=assertion.first(cfg.attribute_username) or "",
                    email=assertion.first(cfg.attribute_email),
                    groups=list(assertion.attributes.get(cfg.attribute_groups, [])),
                    claims={k: v if len(v) != 1 else v[0] for k, v in assertion.attributes.items()},
                    groups_claim=cfg.attribute_groups,
                    username_claim=cfg.attribute_username,
                )
            return await self._grant_login(
                session,
                row=row,
                provider_name=row.name,
                subject_id=assertion.subject,
                name=assertion.first(cfg.attribute_username) or assertion.subject,
                email=assertion.first(cfg.attribute_email),
                groups=assertion.attributes.get(cfg.attribute_groups, []),
                return_to=pending.return_to,
            )

    async def _login_failed(self, session: Any, provider_name: str, reason: str) -> None:
        await audit_log.record(
            session,
            tenant_id=None,
            actor=f"login:{provider_name}",
            action="auth.login_failed",
            diff={"reason": reason},
        )

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
        user_id: UUID | None = None,
        groups: list[str] | None = None,
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
            user_id=user_id,
            name=name,
            email=email,
            groups=list(dict.fromkeys(groups or []))[:MAX_STORED_GROUPS],
            grants=_grants_json(grants),
            grants_computed_at=now,
            break_glass=break_glass,
            created_at=now,
            expires_at=now + (BREAK_GLASS_TTL if break_glass else SESSION_TTL),
            last_seen_at=now,
        )
        session.add(row)
        info = _info(row)
        return LoginResult(cookie, info, return_to)

    # -- sessions ---------------------------------------------------------------------------------

    async def authenticate(self, cookie: str) -> SessionInfo | None:
        """Return the session for a cookie value, or ``None`` (unknown, expired, idle, revoked).

        Grants are recomputed from the stored groups and the current mappings when they are more
        than :data:`GRANTS_MAX_AGE` old; a person left with no grant has the session revoked.

        Raises:
            UserDisabledError: If an administrator disabled the person (the session is revoked).
        """
        return await self._load(cookie, force_recompute=False)

    async def refresh(self, cookie: str) -> SessionInfo | None:
        """Recompute the grants now (``POST /auth/refresh``); the absolute lifetime is unchanged.

        Returns ``None`` if the session is gone, or lost its last grant and was revoked.

        Raises:
            UserDisabledError: If an administrator disabled the person.
        """
        return await self._load(cookie, force_recompute=True)

    async def _load(self, cookie: str, *, force_recompute: bool) -> SessionInfo | None:
        if not cookie.startswith(SESSION_PREFIX):
            return None
        now = self._clock()
        disabled = False
        info: SessionInfo | None = None
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(UserSessionRow)
                    .where(UserSessionRow.token_hash == hash_token(cookie))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            idle = BREAK_GLASS_IDLE if row is not None and row.break_glass else IDLE_TTL
            if row is None or row.expires_at <= now or now - row.last_seen_at > idle:
                return None
            user: UserAccountRow | None = None
            if row.user_id is not None:
                user = await session.get(UserAccountRow, row.user_id)
            if user is not None and user.disabled_at is not None:
                # disabling revokes sessions, but the person is told why, not just "expired"
                row.revoked_at = row.revoked_at or now
                disabled = True
            elif row.revoked_at is not None:
                return None
            elif not row.break_glass and (
                force_recompute or now - row.grants_computed_at > GRANTS_MAX_AGE
            ):
                grants = _distinct(await self.compute_grants(session, row.provider_id, row.groups))
                if not grants:
                    row.revoked_at = now
                    await audit_log.record(
                        session,
                        tenant_id=None,
                        actor=f"user:{row.subject}",
                        action="auth.session_revoked",
                        diff={"reason": "no group grants access any more"},
                    )
                else:
                    row.grants = _grants_json(grants)
                    row.grants_computed_at = now
                    info = _info(row)
            else:
                info = _info(row)
            if info is not None:
                if now - row.last_seen_at > timedelta(seconds=30):
                    row.last_seen_at = now
                if user is not None and now - user.last_seen_at > USER_SEEN_EVERY:
                    user.last_seen_at = now
        if disabled:
            raise UserDisabledError
        return info

    async def explain(self, info: SessionInfo) -> list[GrantVia]:
        """Which provider group produced each grant of this session, under the current mappings."""
        if info.break_glass:
            return []
        async with admin_session(self._db.admin) as session:
            return await self.compute_grants(session, info.provider_id, info.groups)

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
                    totp_confirmed_at=self._clock(),
                )
            )
            await audit_log.record(
                session,
                tenant_id=None,
                actor="system:cli",
                action="auth.local_admin_created",
                object_ref=f"local_admin:{username}",
            )

    async def break_glass_login(
        self, username: str, password: str, code: str, source: str = "unknown"
    ) -> LoginResult:
        """Log the local administrator in. Every attempt is audited and failures back off.

        Failures are counted per source address and per account *from that address* (see
        :mod:`switchyard.services.throttle`), so someone elsewhere cannot lock out the real
        administrator. A TOTP code is accepted once.

        Raises:
            LoginError: With a message that does not say which of the credentials was wrong.
        """
        if not await self.break_glass_on():
            raise LoginError("break-glass login is not enabled")
        rules = [source_rule(source), account_rule(f"bg:{username}", source)]
        if wait := await self.throttle.blocked_for(rules):
            raise LoginError(f"too many failed attempts; try again in {wait} seconds")
        outcome = await self._break_glass_login(username, password, code)
        if isinstance(outcome, str):
            await self.throttle.record_failure(rules)
            raise LoginError(outcome)
        await self.throttle.clear([r.key for r in rules])
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
            ok = False
            step: int | None = None
            if (
                admin is not None
                and admin.totp_confirmed_at is not None
                and verify_password(password, admin.password_hash)
            ):
                try:
                    secret = await self._secrets.get(admin.totp_secret_ref) if self._secrets else ""
                except LookupError:
                    secret = ""
                step = matching_totp_step(secret, code, now.timestamp()) if secret else None
                ok = step is not None and (
                    admin.totp_last_step is None or step > admin.totp_last_step
                )
            elif admin is None or admin.totp_confirmed_at is None:
                verify_password(password, hash_password("x" * 16, n=2**10))  # keep timing similar
            if not ok:
                await audit_log.record(
                    session,
                    tenant_id=None,
                    actor=f"local:{username}",
                    action="auth.break_glass_failed",
                )
                return "invalid credentials"
            assert admin is not None  # noqa: S101
            admin.failed_attempts = 0
            admin.totp_last_step = step
            admin.last_login_at = now
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


def _distinct(grants: list[GrantVia]) -> tuple[tuple[UUID | None, Role], ...]:
    """The (tenant, role) pairs of ``grants`` without duplicates, keeping their order."""
    return tuple(dict.fromkeys((g.tenant_id, g.role) for g in grants))


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
        idle_timeout=BREAK_GLASS_IDLE if row.break_glass else IDLE_TTL,
        grants_computed_at=row.grants_computed_at,
        user_id=row.user_id,
        provider_id=row.provider_id,
        groups=tuple(row.groups),
    )


__all__ = [
    "GrantVia",
    "LoginError",
    "LoginResult",
    "ProviderInfo",
    "SessionInfo",
    "SessionService",
    "TestDone",
    "UserDisabledError",
    "hash_password",
    "safe_return_to",
    "totp_code",
    "verify_password",
    "verify_totp",
]


def _base_of(url: str) -> str:
    """``https://host`` from an absolute URL (used to derive default SAML entity ids)."""
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{rest.split('/', 1)[0]}"


def _canonical_acs(acs_url: str, provider_id: UUID) -> str:
    """The ACS URL with the provider id (never the name) as last segment, so it never varies."""
    return f"{acs_url.rsplit('/', 1)[0]}/{provider_id}"
