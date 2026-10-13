# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Users: everyone who has signed in, what they can do now, and ending their access.

``user_account`` rows are written at sign-in (see :mod:`switchyard.services.sessions`). This module
lists and shows them, and lets a system administrator disable, enable, delete them or end their
sessions at once. A tenant administrator gets the same list limited to people with a grant in their
tenant, showing only that tenant's grants. The break-glass administrator has no row here: its
sessions are audited, not listed.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, func, or_, select, tuple_, update
from sqlalchemy.sql.elements import ColumnElement

from switchyard.db import audit_log
from switchyard.db.auth_models import (
    IdentityProviderRow,
    IdpRoleMappingRow,
    UserAccountRow,
    UserSessionRow,
)
from switchyard.db.models import TenantRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.services.changesets import ROLE_RANK, require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.errors import NotFoundError
from switchyard.services.role_mappings import provider_clause


@dataclass(frozen=True)
class GrantSource:
    """The provider group that produced a grant."""

    provider: str
    group: str


@dataclass(frozen=True)
class EffectiveGrant:
    """What a person can do in one tenant right now (``tenant_id`` is ``None`` for system-admin)."""

    tenant_id: UUID | None
    tenant: str | None
    role: Role
    via: tuple[GrantSource, ...]


@dataclass(frozen=True)
class UserRecord:
    """A person who has signed in."""

    id: UUID
    provider_id: UUID
    provider: str
    subject: str
    username: str
    email: str | None
    groups: tuple[str, ...]
    first_login_at: datetime
    last_login_at: datetime
    last_seen_at: datetime
    disabled_at: datetime | None
    grants: tuple[EffectiveGrant, ...]
    active_sessions: int = 0


@dataclass(frozen=True)
class UserPage:
    """One page of users; ``next_cursor`` is ``None`` on the last page."""

    items: list[UserRecord]
    next_cursor: str | None


def _like_pattern(text: str) -> str:
    """A ``%text%`` LIKE pattern with ``%``, ``_`` and the escape character escaped."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def effective_grants(
    groups: list[str],
    mappings: list[tuple[IdpRoleMappingRow, str]],
    tenants: dict[UUID, str],
    *,
    only_tenant: UUID | None = None,
) -> tuple[EffectiveGrant, ...]:
    """Combine the mappings a person's ``groups`` match into one grant per tenant.

    Roles nest, so the union of the matching mappings in a tenant is the highest role; ``via``
    lists every group that granted something there. A ``system-admin`` mapping gives one grant
    with no tenant. ``only_tenant`` keeps just that tenant's grant.

    Example:
        With mappings ``ops -> operator`` and ``admins -> tenant-admin`` in tenant T, a member
        of both has one grant ``tenant-admin`` in T with ``via`` ``[ops, admins]``.
    """
    wanted = set(groups)
    per_scope: dict[UUID | None, tuple[Role, list[GrantSource]]] = {}
    for mapping, provider_name in mappings:
        if mapping.group_name not in wanted:
            continue
        if only_tenant is not None and mapping.scope_tenant_id != only_tenant:
            continue
        role = Role(mapping.role)
        current_role, via = per_scope.get(mapping.scope_tenant_id, (role, []))
        if ROLE_RANK[role] > ROLE_RANK[current_role]:
            current_role = role
        via.append(GrantSource(provider_name, mapping.group_name))
        per_scope[mapping.scope_tenant_id] = (current_role, via)
    out = [
        EffectiveGrant(tenant_id, tenants.get(tenant_id) if tenant_id else None, role, tuple(via))
        for tenant_id, (role, via) in per_scope.items()
    ]
    out.sort(key=lambda g: (g.tenant is not None, g.tenant or ""))
    return tuple(out)


class UserService:
    """List, show and administer users."""

    def __init__(self, db: Databases, clock: Clock = utcnow) -> None:
        """Create the service."""
        self._db = db
        self._clock = clock

    async def list_users(
        self,
        principal: Principal,
        *,
        tenant_id: UUID | None = None,
        q: str | None = None,
        provider: str | None = None,
        tenant_filter: UUID | None = None,
        disabled: bool | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> UserPage:
        """List users, most recent sign-in first.

        With ``tenant_id`` (the ``/tenants/{t}/users`` route) the list is limited to people with
        a grant in that tenant and shows only that tenant's grants (tenant administrators). Without
        it, all users (system administrators).

        Raises:
            ValueError: If ``cursor`` is not one this method returned.
        """
        if tenant_id is None:
            require(principal, "manage", "user")
        else:
            if not principal.can_access(tenant_id):
                raise NotFoundError("tenant not found")
            require(principal, "list", "tenant_user")
        scope = tenant_id or tenant_filter
        query = select(UserAccountRow, IdentityProviderRow.name).join(
            IdentityProviderRow, IdentityProviderRow.id == UserAccountRow.provider_id
        )
        if scope is not None:
            query = query.where(_has_grant_in(scope))
        if provider is not None:
            query = query.where(provider_clause(provider))
        if disabled is not None:
            query = query.where(
                UserAccountRow.disabled_at.is_not(None)
                if disabled
                else UserAccountRow.disabled_at.is_(None)
            )
        if q:
            pattern = _like_pattern(q)
            query = query.where(
                or_(
                    UserAccountRow.username.ilike(pattern, escape="\\"),
                    UserAccountRow.email.ilike(pattern, escape="\\"),
                )
            )
        if cursor:
            try:
                stamp, _, last_id = cursor.partition("|")
                after = (datetime.fromisoformat(stamp), UUID(last_id))
            except ValueError as exc:
                raise ValueError("cursor is not valid") from exc
            query = query.where(tuple_(UserAccountRow.last_login_at, UserAccountRow.id) < after)
        async with admin_session(self._db.admin) as session:
            rows = (
                await session.execute(
                    query.order_by(
                        UserAccountRow.last_login_at.desc(), UserAccountRow.id.desc()
                    ).limit(limit + 1)
                )
            ).all()
            more = len(rows) > limit
            rows = rows[:limit]
            records = await self._records(session, rows, only_tenant=tenant_id)
        nxt = f"{rows[-1][0].last_login_at.isoformat()}|{rows[-1][0].id}" if more else None
        return UserPage(records, nxt)

    async def get(self, principal: Principal, user_id: UUID) -> UserRecord:
        """One user with effective grants and live-session count (system administrators)."""
        require(principal, "manage", "user")
        async with admin_session(self._db.admin) as session:
            row = await self._row(session, user_id)
            (record,) = await self._records(session, [row], with_sessions=True)
            return record

    async def set_disabled(
        self, principal: Principal, user_id: UUID, disabled: bool, *, request_id: str | None = None
    ) -> UserRecord:
        """Disable (ending every session at once) or enable a user."""
        require(principal, "manage", "user")
        now = self._clock()
        async with admin_session(self._db.admin) as session:
            row = await self._row(session, user_id)
            account: UserAccountRow = row[0]
            account.disabled_at = now if disabled else None
            ended = 0
            if disabled:
                ended = await self._revoke(session, user_id, now)
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="user.disable" if disabled else "user.enable",
                object_ref=f"user:{user_id}",
                diff={"username": account.username, "sessions_ended": ended},
                request_id=request_id,
            )
            (record,) = await self._records(session, [row], with_sessions=True)
            return record

    async def revoke_sessions(
        self, principal: Principal, user_id: UUID, *, request_id: str | None = None
    ) -> int:
        """End every session of a user now; returns how many were live."""
        require(principal, "manage", "user")
        async with admin_session(self._db.admin) as session:
            account: UserAccountRow = (await self._row(session, user_id))[0]
            ended = await self._revoke(session, user_id, self._clock())
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="user.revoke_sessions",
                object_ref=f"user:{user_id}",
                diff={"username": account.username, "sessions_ended": ended},
                request_id=request_id,
            )
            return ended

    async def delete(
        self, principal: Principal, user_id: UUID, *, request_id: str | None = None
    ) -> None:
        """Delete the user and their sessions. The person reappears at their next sign-in."""
        require(principal, "manage", "user")
        async with admin_session(self._db.admin) as session:
            account: UserAccountRow = (await self._row(session, user_id))[0]
            username = account.username
            await session.delete(account)
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="user.delete",
                object_ref=f"user:{user_id}",
                diff={"username": username},
                request_id=request_id,
            )

    # -- helpers ----------------------------------------------------------------------------------

    @staticmethod
    async def _row(session: Any, user_id: UUID) -> Any:
        found = (
            await session.execute(
                select(UserAccountRow, IdentityProviderRow.name)
                .join(IdentityProviderRow, IdentityProviderRow.id == UserAccountRow.provider_id)
                .where(UserAccountRow.id == user_id)
                .with_for_update(of=UserAccountRow)
            )
        ).first()
        if found is None:
            raise NotFoundError("user not found")
        return found

    @staticmethod
    async def _revoke(session: Any, user_id: UUID, now: datetime) -> int:
        result = await session.execute(
            update(UserSessionRow)
            .where(UserSessionRow.user_id == user_id, UserSessionRow.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        return int(result.rowcount)

    async def _records(
        self,
        session: Any,
        rows: Sequence[Any],
        *,
        only_tenant: UUID | None = None,
        with_sessions: bool = False,
    ) -> list[UserRecord]:
        if not rows:
            return []
        provider_ids = {r[0].provider_id for r in rows}
        mappings = [
            (m, name)
            for m, name in (
                await session.execute(
                    select(IdpRoleMappingRow, IdentityProviderRow.name)
                    .join(
                        IdentityProviderRow, IdentityProviderRow.id == IdpRoleMappingRow.provider_id
                    )
                    .where(IdpRoleMappingRow.provider_id.in_(provider_ids))
                )
            ).all()
        ]
        tenants = {t.id: t.name for t in (await session.execute(select(TenantRow))).scalars()}
        live: dict[UUID, int] = {}
        if with_sessions:
            now = self._clock()
            counts = await session.execute(
                select(UserSessionRow.user_id, func.count())
                .where(
                    UserSessionRow.user_id.in_([r[0].id for r in rows]),
                    UserSessionRow.revoked_at.is_(None),
                    UserSessionRow.expires_at > now,
                )
                .group_by(UserSessionRow.user_id)
            )
            live = dict(counts.all())
        out: list[UserRecord] = []
        for account, provider_name in rows:
            own = [(m, n) for m, n in mappings if m.provider_id == account.provider_id]
            grants = effective_grants(account.groups, own, tenants, only_tenant=only_tenant)
            out.append(
                UserRecord(
                    id=account.id,
                    provider_id=account.provider_id,
                    provider=provider_name,
                    subject=account.subject,
                    username=account.username,
                    email=account.email,
                    # a tenant administrator sees groups only through that tenant's grants
                    groups=() if only_tenant is not None else tuple(account.groups),
                    first_login_at=account.first_login_at,
                    last_login_at=account.last_login_at,
                    last_seen_at=account.last_seen_at,
                    disabled_at=account.disabled_at,
                    grants=grants,
                    active_sessions=live.get(account.id, 0),
                )
            )
        return out


def _has_grant_in(tenant_id: UUID) -> ColumnElement[bool]:
    """SQL: the user's stored groups match a mapping of ``tenant_id`` at their provider."""
    return (
        select(IdpRoleMappingRow.id)
        .where(
            and_(
                IdpRoleMappingRow.provider_id == UserAccountRow.provider_id,
                IdpRoleMappingRow.scope_tenant_id == tenant_id,
                UserAccountRow.groups.has_key(IdpRoleMappingRow.group_name),
            )
        )
        .exists()
    )


__all__ = [
    "EffectiveGrant",
    "GrantSource",
    "UserPage",
    "UserRecord",
    "UserService",
    "effective_grants",
]
