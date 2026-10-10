"""API tokens: hashed, scoped, expiring machine credentials.

A token looks like ``sy_<22 url-safe characters>``. Only its SHA-256 hash is stored, so a database
leak does not leak working tokens; the plaintext is shown once, when it is created. Tokens belong to
one tenant and carry one role, except ``system-admin`` tokens, which belong to no tenant.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update

from switchyard.db import audit_log
from switchyard.db.ops_models import ApiTokenRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.validators import validate_name
from switchyard.domain.workflow import Role
from switchyard.ids import uuid7
from switchyard.services.changesets import ROLE_RANK, require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.errors import NotFoundError, PermissionDeniedError

PREFIX = "sy_"
SYSTEM_DEFAULT_TTL = timedelta(days=30)
SYSTEM_MAX_TTL = timedelta(days=90)
#: A system token's use is audited at most this often (every call would flood the log).
SYSTEM_USE_AUDIT_EVERY = timedelta(minutes=5)


def hash_token(token: str) -> str:
    """Return the stored form of a token.

    Example:
        >>> len(hash_token("sy_example"))
        64
    """
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class TokenRecord:
    """A token without its secret."""

    id: UUID
    tenant_id: UUID | None
    name: str
    role: Role
    expires_at: datetime | None
    created_by: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


def _record(row: ApiTokenRow) -> TokenRecord:
    return TokenRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        role=Role(row.role),
        expires_at=row.expires_at,
        created_by=row.created_by,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


class TokenService:
    """Creates, lists, revokes and verifies API tokens."""

    def __init__(self, db: Databases, clock: Clock = utcnow) -> None:
        """Create the service."""
        self._db = db
        self._clock = clock

    async def create(
        self,
        principal: Principal,
        tenant_id: UUID | None,
        name: str,
        role: Role,
        *,
        ttl: timedelta | None = timedelta(days=90),
        request_id: str | None = None,
    ) -> tuple[str, TokenRecord]:
        """Create a token and return ``(plaintext, record)``. The plaintext is not recoverable.

        A tenant administrator may issue tokens up to their own role inside their tenant. A system
        token (``tenant_id=None``, role ``system-admin``) needs a system administrator.
        """
        validate_name(name)
        if principal.actor.startswith("token:"):
            raise PermissionDeniedError("an API token cannot create tokens")
        if role is Role.SYSTEM_ADMIN:
            require(principal, "manage", "system_token")
            if tenant_id is not None:
                raise ValueError(
                    "system-admin tokens belong to no tenant; create one with POST /system/tokens"
                )
        else:
            if tenant_id is None:
                raise ValueError("this role needs a tenant")
            if not principal.can_access(tenant_id):
                raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
            require(principal, "manage", "token")
            if ROLE_RANK[role] > ROLE_RANK[principal.role]:
                raise PermissionDeniedError("cannot issue a token above your own role")
        plaintext = PREFIX + secrets.token_urlsafe(16)
        async with admin_session(self._db.admin) as session:
            row = ApiTokenRow(
                id=uuid7(),
                tenant_id=tenant_id,
                name=name,
                token_hash=hash_token(plaintext),
                role=role.value,
                expires_at=self._clock() + ttl if ttl else None,
                created_by=principal.actor,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="token.create",
                object_ref=f"token:{name}",
                diff={"role": role.value},
                request_id=request_id,
            )
            return plaintext, _record(row)

    async def create_system(
        self,
        principal: Principal,
        name: str,
        *,
        ttl: timedelta = SYSTEM_DEFAULT_TTL,
        request_id: str | None = None,
    ) -> tuple[str, TokenRecord]:
        """Create a system-admin token (system administrators): 30 days by default, 90 at most.

        Raises:
            ValueError: If ``ttl`` is longer than 90 days or not positive.
        """
        require(principal, "manage", "system_token")
        if not timedelta(0) < ttl <= SYSTEM_MAX_TTL:
            raise ValueError("a system token lasts between 1 and 90 days")
        return await self.create(
            principal, None, name, Role.SYSTEM_ADMIN, ttl=ttl, request_id=request_id
        )

    async def list_system(self, principal: Principal) -> list[TokenRecord]:
        """System tokens (those with no tenant), oldest first."""
        require(principal, "manage", "system_token")
        async with admin_session(self._db.admin) as session:
            rows = (
                await session.execute(
                    select(ApiTokenRow)
                    .where(ApiTokenRow.tenant_id.is_(None))
                    .order_by(ApiTokenRow.created_at)
                )
            ).scalars()
            return [_record(r) for r in rows]

    async def revoke_system(
        self, principal: Principal, token_id: UUID, *, request_id: str | None = None
    ) -> None:
        """Revoke a system token; it stops working at once."""
        require(principal, "manage", "system_token")
        async with admin_session(self._db.admin) as session:
            result = await session.execute(
                update(ApiTokenRow)
                .where(
                    ApiTokenRow.id == token_id,
                    ApiTokenRow.tenant_id.is_(None),
                    ApiTokenRow.revoked_at.is_(None),
                )
                .values(revoked_at=func.now())
            )
            if result.rowcount == 0:  # type: ignore[attr-defined]
                raise NotFoundError("system token not found or already revoked")
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="token.revoke",
                object_ref=f"token:{token_id}",
                diff={"system": True},
                request_id=request_id,
            )

    async def list_tokens(self, principal: Principal, tenant_id: UUID) -> list[TokenRecord]:
        """List a tenant's tokens (tenant administrators and up)."""
        if not principal.can_access(tenant_id):
            raise NotFoundError("tenant not found")
        require(principal, "manage", "token")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = (
                await session.execute(select(ApiTokenRow).order_by(ApiTokenRow.created_at))
            ).scalars()
            return [_record(r) for r in rows]

    async def revoke(
        self,
        principal: Principal,
        tenant_id: UUID,
        token_id: UUID,
        *,
        request_id: str | None = None,
    ) -> None:
        """Revoke a token. Revoked tokens stop working immediately."""
        if not principal.can_access(tenant_id):
            raise NotFoundError("tenant not found")
        require(principal, "manage", "token")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            result = await session.execute(
                update(ApiTokenRow)
                .where(ApiTokenRow.id == token_id, ApiTokenRow.revoked_at.is_(None))
                .values(revoked_at=func.now())
            )
            if result.rowcount == 0:  # type: ignore[attr-defined]
                raise NotFoundError("token not found or already revoked")
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="token.revoke",
                object_ref=f"token:{token_id}",
                request_id=request_id,
            )

    async def authenticate(self, token: str) -> Principal | None:
        """Return the principal for a valid token, or ``None``.

        ``None`` covers unknown, revoked and expired tokens alike, so a caller learns nothing about
        which it was.
        """
        if not token.startswith(PREFIX):
            return None
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(ApiTokenRow).where(ApiTokenRow.token_hash == hash_token(token))
                )
            ).scalar_one_or_none()
            if row is None or row.revoked_at is not None:
                return None
            if row.expires_at is not None and row.expires_at <= self._clock():
                return None
            now = self._clock()
            if row.tenant_id is None and (
                row.last_used_at is None or now - row.last_used_at > SYSTEM_USE_AUDIT_EVERY
            ):
                await audit_log.record(
                    session,
                    tenant_id=None,
                    actor=f"token:{row.name}",
                    action="token.system_used",
                    object_ref=f"token:{row.id}",
                )
            await session.execute(
                update(ApiTokenRow).where(ApiTokenRow.id == row.id).values(last_used_at=func.now())
            )
            role = Role(row.role)
            tenants = frozenset({row.tenant_id}) if row.tenant_id else frozenset()
            return Principal(actor=f"token:{row.name}", role=role, tenant_ids=tenants)
