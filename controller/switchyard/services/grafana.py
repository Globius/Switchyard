"""Embedded Grafana: launch tickets, proxy sessions and per-tenant Grafana organizations (ADR 0085).

The flow, in one place:

1. A signed-in person asks to open Grafana for a tenant (or, for a system administrator, for all
   of them). :meth:`GrafanaService.launch` checks the role and stores a *ticket* that works once,
   for :data:`TICKET_TTL`.
2. The browser presents the ticket to the controller's Grafana proxy. :meth:`GrafanaService.redeem`
   spends it, makes sure the tenant's Grafana organization, data sources, dashboards and the
   person's Grafana user exist, and issues a *proxy session* (a random cookie, stored as SHA-256)
   for that one tenant, good for :data:`SESSION_TTL` after its last use and at most
   :data:`SESSION_MAX` in all.
3. Every Grafana request carries the cookie; :meth:`GrafanaService.authenticate` maps it to the
   Grafana login and organization the proxy forwards as trusted headers. Grafana itself accepts no
   other credential.

Tenant isolation does not depend on any of that. The tenant organization's data sources point at the
controller's scoped data source proxy and authenticate with a token derived from
:attr:`GrafanaConfig.ds_secret` that only identifies the tenant (:meth:`GrafanaService.ds_token`),
so whoever ends up inside Grafana can only read what the controller will return for that tenant.
"""

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from importlib import resources
from typing import Any
from uuid import UUID

from sqlalchemy import select, update

from switchyard.adapters.grafana import MAIN_ORG_ID, DatasourceSpec, GrafanaAdmin
from switchyard.db import audit_log
from switchyard.db.auth_models import GrafanaGrantRow, UserSessionRow
from switchyard.db.tenancy import admin_session
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.errors import PermissionDeniedError
from switchyard.services.tokens import hash_token

TICKET_PREFIX = "sygt_"
SESSION_PREFIX = "sygs_"
DS_TOKEN_PREFIX = "sygd_"  # noqa: S105 (a prefix, not a secret)
TICKET_TTL = timedelta(seconds=60)
#: The proxy session is idle-limited: each use pushes the end out to this far ahead ...
SESSION_TTL = timedelta(minutes=30)
#: ... but never beyond this long after it started, nor beyond the browser session.
SESSION_MAX = timedelta(hours=8)
#: The end is only moved (one database write) when this much of the TTL has been used.
RENEW_AFTER = timedelta(minutes=10)
#: How long a tenant's organization, data sources and dashboards are assumed to be in place.
SYNC_EVERY = 300.0
MIN_SECRET_LENGTH = 32
HOME_DASHBOARD = "sy-tenant-services"

#: Where a launch may land inside Grafana: home, a dashboard, Explore or the dashboard list.
_LANDING = re.compile(
    r"^(?:|/d/[A-Za-z0-9_-]{1,64}(?:/[A-Za-z0-9_.-]{0,128})?|/explore|/dashboards)"
    r"(?:\?[A-Za-z0-9_=&%.\-]{0,256})?$"
)


@dataclass(frozen=True)
class GrafanaConfig:
    """Everything the embedding needs to know about the deployment.

    Attributes:
        internal_url: Where the controller reaches Grafana (``http://grafana:3000``). The proxy
            strips ``/grafana`` before Grafana sees a path. Grafana is published nowhere else.
        admin_user: Grafana server administrator the controller manages organizations as.
        admin_password: Its password.
        ds_secret: Key that derives the per-tenant data source tokens; at least
            :data:`MIN_SECRET_LENGTH` characters. Changing it re-keys every tenant at the next
            launch.
        datasource_url: Where Grafana reaches the controller's data source proxy
            (``http://api:8000/internal/ds``); not routed through the public front.
        prometheus_url: Prometheus, behind the data source proxy.
        loki_url: Loki, behind the data source proxy; none disables log panels.
    """

    internal_url: str
    admin_user: str
    admin_password: str
    ds_secret: str
    datasource_url: str
    prometheus_url: str
    loki_url: str | None = None


@dataclass(frozen=True)
class Launch:
    """A ticket the browser can present once."""

    ticket: str
    expires_at: datetime
    tenant_id: UUID | None


@dataclass(frozen=True)
class ProxySession:
    """What a redeemed ticket gives the browser."""

    cookie: str
    expires_at: datetime
    landing: str


@dataclass(frozen=True)
class GrantInfo:
    """A valid proxy session: who Grafana should see and which organization they are in."""

    login: str
    name: str
    org_id: int
    tenant_id: UUID | None
    expires_at: datetime
    renewed: bool = False  # this request moved the end out: the cookie should be re-sent


class GrafanaLaunchError(Exception):
    """The ticket is unknown, expired or already used, or Grafana could not be prepared."""


def valid_landing(path: str) -> bool:
    """Whether ``path`` is somewhere a launch may land (never an absolute URL or a ``//`` path).

    Example:
        >>> valid_landing("/d/sy-tenant-services/virtual-services?var-service=vs_a")
        True
        >>> valid_landing("//evil.example")
        False
        >>> valid_landing("/api/admin/users")
        False
    """
    return _LANDING.match(path) is not None


def grafana_login(subject: str, tenant_id: UUID | None) -> str:
    """The Grafana login of a person in one tenant (``sy-<hash>``; no personal data in Grafana).

    Example:
        >>> grafana_login("user:alice", None) != grafana_login("user:alice", UUID(int=1))
        True
    """
    digest = hashlib.sha256(f"{subject}|{tenant_id or 'all'}".encode()).hexdigest()[:24]
    return f"sy-{digest}"


def _load_dashboard(name: str) -> dict[str, Any]:
    text = resources.files("switchyard.assets.grafana").joinpath(name).read_text("utf-8")
    loaded: dict[str, Any] = json.loads(text)
    return loaded


class GrafanaService:
    """Tickets, proxy sessions and the Grafana state they need."""

    def __init__(
        self,
        db: Databases,
        config: GrafanaConfig,
        admin: GrafanaAdmin,
        clock: Clock = utcnow,
    ) -> None:
        """Create the service.

        Raises:
            ValueError: If ``config.ds_secret`` is too short to be a key.
        """
        if len(config.ds_secret) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"SWITCHYARD_GRAFANA_DS_SECRET must be at least {MIN_SECRET_LENGTH} characters"
            )
        self._db = db
        self.config = config
        self._admin = admin
        self._clock = clock
        self._synced: dict[UUID, tuple[float, int]] = {}

    # -- data source tokens -----------------------------------------------------------------------

    def ds_token(self, tenant_id: UUID) -> str:
        """The token Grafana sends to the data source proxy for one tenant.

        It names the tenant and proves the controller issued it; it carries no user and no role.

        Example:
            >>> from switchyard.services.grafana import GrafanaConfig
            >>> cfg = GrafanaConfig("http://g", "admin", "pw", "k" * 32, "http://c", "http://p")
            >>> svc = GrafanaService(None, cfg, None)  # type: ignore[arg-type]
            >>> svc.tenant_for_ds_token(svc.ds_token(UUID(int=7))) == UUID(int=7)
            True
        """
        return f"{DS_TOKEN_PREFIX}{tenant_id.hex}.{self._mac(tenant_id)}"

    def _mac(self, tenant_id: UUID) -> str:
        return hmac.new(
            self.config.ds_secret.encode(), f"grafana-ds:{tenant_id}".encode(), hashlib.sha256
        ).hexdigest()[:48]

    def tenant_for_ds_token(self, token: str) -> UUID | None:
        """The tenant a data source token was issued for, or ``None`` if it is not ours."""
        if not token.startswith(DS_TOKEN_PREFIX):
            return None
        body = token[len(DS_TOKEN_PREFIX) :]
        hex_id, _, mac = body.partition(".")
        try:
            tenant_id = UUID(hex=hex_id)
        except ValueError:
            return None
        if not hmac.compare_digest(mac.encode(), self._mac(tenant_id).encode()):
            return None
        return tenant_id

    # -- launch and redeem ------------------------------------------------------------------------

    async def launch(
        self,
        principal: Principal,
        tenant_id: UUID | None,
        *,
        user_session_id: UUID | None,
        landing: str = "",
        request_id: str | None = None,
    ) -> Launch:
        """Issue a single-use ticket.

        Args:
            principal: The signed-in person, narrowed to ``tenant_id`` when there is one.
            tenant_id: The tenant to open; ``None`` is the unscoped view for system administrators.
            user_session_id: The browser session the grant is tied to (it ends with it).
            landing: A path inside Grafana (see :func:`valid_landing`).
            request_id: For the audit event.

        Raises:
            PermissionDeniedError: If the person may not read the tenant, or is not a system
                administrator and asked for the unscoped view.
            GrafanaLaunchError: If ``landing`` is not an allowed path.
        """
        if not valid_landing(landing):
            raise GrafanaLaunchError("that Grafana page is not available from here")
        if tenant_id is None:
            if not principal.is_system:
                raise PermissionDeniedError(f"{principal.actor} needs the system-admin role")
        else:
            if not principal.can_access(tenant_id):
                raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
            require(principal, "read", "tenant_data")
        ticket = TICKET_PREFIX + secrets.token_urlsafe(32)
        now = self._clock()
        expires = now + TICKET_TTL
        async with admin_session(self._db.admin) as session:
            session.add(
                GrafanaGrantRow(
                    id=uuid7(),
                    ticket_hash=hash_token(ticket),
                    subject=principal.actor,
                    display_name=principal.actor,
                    scope_tenant_id=tenant_id,
                    user_session_id=user_session_id,
                    landing=landing,
                    ticket_expires_at=expires,
                )
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="grafana.launch",
                object_ref=f"tenant:{tenant_id}" if tenant_id else "tenant:*",
                request_id=request_id,
            )
        return Launch(ticket=ticket, expires_at=expires, tenant_id=tenant_id)

    async def redeem(self, ticket: str) -> ProxySession:
        """Spend a ticket and start the proxy session.

        The ticket is consumed by one conditional ``UPDATE`` before anything else happens, so two
        requests (or two controller nodes) racing with the same ticket cannot both win. Grafana is
        prepared after that; a failure there leaves the ticket spent and the person starts again.

        Raises:
            GrafanaLaunchError: If the ticket is unknown, expired, used, or Grafana is unavailable.
        """
        if not ticket.startswith(TICKET_PREFIX):
            raise GrafanaLaunchError("invalid Grafana ticket")
        now = self._clock()
        cookie = SESSION_PREFIX + secrets.token_urlsafe(32)
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    update(GrafanaGrantRow)
                    .where(
                        GrafanaGrantRow.ticket_hash == hash_token(ticket),
                        GrafanaGrantRow.redeemed_at.is_(None),
                        GrafanaGrantRow.ticket_expires_at > now,
                    )
                    .values(redeemed_at=now)
                    .returning(GrafanaGrantRow)
                )
            ).scalar_one_or_none()
            if row is None:
                raise GrafanaLaunchError("this Grafana link has expired or was already used")
            grant_id, tenant_id, subject = row.id, row.scope_tenant_id, row.subject
            name, landing = row.display_name, row.landing
            expires = now + SESSION_TTL
            if row.user_session_id is not None:
                started = await session.get(UserSessionRow, row.user_session_id)
                if started is not None:
                    expires = min(expires, started.expires_at)
        # The ticket is spent (committed) before Grafana is touched; if Grafana fails the person
        # simply starts again.
        login = grafana_login(subject, tenant_id)
        try:
            org_id = await self._prepare(tenant_id, login, name)
        except Exception as exc:
            raise GrafanaLaunchError(f"Grafana is not available: {exc}") from exc
        async with admin_session(self._db.admin) as session:
            await session.execute(
                update(GrafanaGrantRow)
                .where(GrafanaGrantRow.id == grant_id)
                .values(
                    session_hash=hash_token(cookie),
                    session_expires_at=expires,
                    grafana_login=login,
                    grafana_org_id=org_id,
                )
            )
        return ProxySession(cookie=cookie, expires_at=expires, landing=landing)

    async def _prepare(self, tenant_id: UUID | None, login: str, name: str) -> int:
        """Make sure the organization, its contents and the person's Grafana user exist."""
        org_id = MAIN_ORG_ID if tenant_id is None else await self._ensure_tenant(tenant_id)
        await self._admin.ensure_user(login, name, f"{login}@switchyard.invalid")
        await self._admin.confine_user(org_id, login, "Viewer")
        return org_id

    async def _ensure_tenant(self, tenant_id: UUID) -> int:
        cached = self._synced.get(tenant_id)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        org_id = await self._admin.ensure_org(f"sy-{tenant_id}")
        token = self.ds_token(tenant_id)
        base = self.config.datasource_url.rstrip("/")
        specs = [
            DatasourceSpec(
                "prometheus", "Prometheus", "prometheus", f"{base}/prometheus", token, True
            )
        ]
        if self.config.loki_url:
            specs.append(DatasourceSpec("loki", "Loki", "loki", f"{base}/loki", token))
        await self._admin.ensure_datasources(org_id, specs)
        await self._admin.ensure_dashboards(
            org_id, [_load_dashboard("tenant-virtual-services.json")], HOME_DASHBOARD
        )
        self._synced[tenant_id] = (time.monotonic() + SYNC_EVERY, org_id)
        return org_id

    # -- proxy sessions ---------------------------------------------------------------------------

    async def authenticate(self, cookie: str) -> GrantInfo | None:
        """The grant behind a proxy cookie, or ``None`` (unknown, expired, or its login ended)."""
        if not cookie.startswith(SESSION_PREFIX):
            return None
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(GrafanaGrantRow).where(
                        GrafanaGrantRow.session_hash == hash_token(cookie),
                        GrafanaGrantRow.session_expires_at > now,
                    )
                )
            ).scalar_one_or_none()
            if row is None or row.grafana_login is None or row.grafana_org_id is None:
                return None
            started: UserSessionRow | None = None
            if row.user_session_id is not None:
                started = await session.get(UserSessionRow, row.user_session_id)
                if started is None or started.revoked_at is not None or started.expires_at <= now:
                    return None
            expires = row.session_expires_at or now
            renewed = False
            if row.redeemed_at is not None and expires - now < SESSION_TTL - RENEW_AFTER:
                cap = row.redeemed_at + SESSION_MAX
                if row.user_session_id is not None and started is not None:
                    cap = min(cap, started.expires_at)
                later = min(now + SESSION_TTL, cap)
                if later > expires:
                    row.session_expires_at = expires = later
                    renewed = True
            return GrantInfo(
                login=row.grafana_login,
                name=row.display_name,
                org_id=row.grafana_org_id,
                tenant_id=row.scope_tenant_id,
                expires_at=expires,
                renewed=renewed,
            )
