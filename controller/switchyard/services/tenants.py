# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Tenants: created and changed by system administrators (outside change sets).

Tenants are the root of ownership and are not part of any tenant's own state, so they are managed
directly with the admin database role and audited, not through change sets.
"""

import contextlib
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import and_, delete, func, select

from switchyard.db import audit_log
from switchyard.db import models as m
from switchyard.db.auth_models import IdpRoleMappingRow, UserAccountRow
from switchyard.db.models import TenantRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.changes import STICK_TABLE_QUOTA, ObjectType
from switchyard.domain.validators import validate_name
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
)

#: Tables counted for quota usage, by quota key (the object type name).
_USAGE_TABLES: dict[ObjectType, Any] = {
    ObjectType.VIRTUAL_SERVICE: m.VirtualServiceRow,
    ObjectType.POOL: m.PoolRow,
    ObjectType.POLICY_SET: m.PolicySetRow,
    ObjectType.CERTIFICATE: m.CertificateRow,
    ObjectType.HEALTH_MONITOR: m.HealthMonitorRow,
    ObjectType.VIP_POOL: m.VipPoolRow,
    ObjectType.SE_GROUP: m.SeGroupRow,
    ObjectType.NETWORK: m.NetworkRow,
    ObjectType.ROUTING_PROFILE: m.RoutingProfileRow,
}
_OBJECT_TABLES = tuple(_USAGE_TABLES.values())


def validate_quotas(quotas: dict[str, int]) -> dict[str, int]:
    """Check quota keys (object type names or ``stick_table_entries``) and non-negative limits.

    Example:
        >>> validate_quotas({"pool": 5})
        {'pool': 5}
    """
    valid = {t.value for t in ObjectType} | {STICK_TABLE_QUOTA}
    for key, limit in quotas.items():
        if key not in valid:
            raise ValueError(f"unknown quota {key!r}; use one of {', '.join(sorted(valid))}")
        if limit < 0:
            raise ValueError(f"quota {key!r} must not be negative")
    return quotas


@dataclass(frozen=True)
class TenantRecord:
    """A tenant."""

    id: UUID
    name: str
    version: int
    quotas: dict[str, int]
    require_approval: bool
    usage: dict[str, int] = field(default_factory=dict)
    members: int = 0


def _record(row: TenantRow, usage: dict[str, int] | None = None, members: int = 0) -> TenantRecord:
    return TenantRecord(
        id=row.id,
        name=row.name,
        version=row.version,
        quotas=dict(row.quotas),
        require_approval=row.require_approval,
        usage=usage if usage is not None else {t.value: 0 for t in _USAGE_TABLES},
        members=members,
    )


async def _members(session: Any, tenant_ids: list[UUID]) -> dict[UUID, int]:
    """Count the people with a grant in each tenant.

    A person counts once per tenant when one of their stored groups matches a role mapping of that
    tenant at their provider, the same rule as ``GET /tenants/{t}/users``. Disabled accounts are
    counted too, because their grants are still there.
    """
    rows = await session.execute(
        select(
            IdpRoleMappingRow.scope_tenant_id,
            func.count(UserAccountRow.id.distinct()),
        )
        .join(
            UserAccountRow,
            and_(
                UserAccountRow.provider_id == IdpRoleMappingRow.provider_id,
                UserAccountRow.groups.has_key(IdpRoleMappingRow.group_name),
            ),
        )
        .where(IdpRoleMappingRow.scope_tenant_id.in_(tenant_ids))
        .group_by(IdpRoleMappingRow.scope_tenant_id)
    )
    return dict(rows.all())


async def _usage(session: Any, tenant_ids: list[UUID]) -> dict[UUID, dict[str, int]]:
    """Count each tenant's objects per type, zeros included."""
    out = {t: {k.value: 0 for k in _USAGE_TABLES} for t in tenant_ids}
    for object_type, table in _USAGE_TABLES.items():
        rows = await session.execute(
            select(table.tenant_id, func.count())
            .where(table.tenant_id.in_(tenant_ids))
            .group_by(table.tenant_id)
        )
        for tenant_id, count in rows:
            out[tenant_id][object_type.value] = count
    return out


class TenantService:
    """Create, read, update and delete tenants."""

    def __init__(self, db: Databases) -> None:
        """Create the service."""
        self._db = db

    async def resolve(self, principal: Principal, ref: str) -> TenantRecord:
        """Find a tenant by id or name and check the principal may see it.

        A tenant the principal cannot access is reported as not found, so tenant names are not
        revealed to outsiders.
        """
        async with admin_session(self._db.admin) as session:
            row = None
            with contextlib.suppress(ValueError):
                row = (
                    await session.execute(select(TenantRow).where(TenantRow.id == UUID(ref)))
                ).scalar_one_or_none()
            if row is None:
                row = (
                    await session.execute(select(TenantRow).where(TenantRow.name == ref))
                ).scalar_one_or_none()
            if row is not None and principal.can_access(row.id):
                return _record(
                    row,
                    (await _usage(session, [row.id]))[row.id],
                    (await _members(session, [row.id])).get(row.id, 0),
                )
        raise NotFoundError(f"tenant {ref!r} not found")

    async def list_tenants(self, principal: Principal) -> list[TenantRecord]:
        """List the tenants the principal can access."""
        async with admin_session(self._db.admin) as session:
            query = select(TenantRow).order_by(TenantRow.name)
            if not principal.is_system:
                query = query.where(TenantRow.id.in_(sorted(principal.tenant_ids)))
            rows = list((await session.execute(query)).scalars())
            usage = await _usage(session, [r.id for r in rows])
            members = await _members(session, [r.id for r in rows])
            return [_record(r, usage[r.id], members.get(r.id, 0)) for r in rows]

    async def create(
        self,
        principal: Principal,
        name: str,
        *,
        quotas: dict[str, int] | None = None,
        require_approval: bool = False,
        request_id: str | None = None,
    ) -> TenantRecord:
        """Create a tenant (system administrators only)."""
        require(principal, "manage", "tenant")
        validate_name(name)
        validate_quotas(quotas or {})
        async with admin_session(self._db.admin) as session:
            if (await session.execute(select(TenantRow.id).where(TenantRow.name == name))).first():
                raise AlreadyExistsError(f"tenant {name!r} already exists")
            row = TenantRow(
                id=uuid7(),
                name=name,
                quotas=quotas or {},
                require_approval=require_approval,
            )
            session.add(row)
            await session.flush()
            await audit_log.record(
                session,
                tenant_id=row.id,
                actor=principal.actor,
                action="tenant.create",
                object_ref=f"tenant:{name}",
                request_id=request_id,
            )
            return _record(row)

    async def update(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        expected_version: int,
        changes: dict[str, Any],
        request_id: str | None = None,
    ) -> TenantRecord:
        """Change ``quotas`` or ``require_approval``.

        System administrators may change any of them; tenant administrators may only toggle
        ``require_approval`` for their own tenant (turning approval *on* is always allowed; turning
        it off needs a system administrator, so a compromised tenant account cannot drop review).
        """
        allowed = {"quotas", "require_approval"}
        if unknown := set(changes) - allowed:
            raise ValueError(f"cannot change: {', '.join(sorted(unknown))}")
        if changes.get("quotas") is not None:
            validate_quotas(changes["quotas"])
        if not principal.can_access(tenant_id):
            raise NotFoundError("tenant not found")
        if not principal.is_system:
            require(principal, "enable", "require_approval")
            if set(changes) - {"require_approval"}:
                require(principal, "change", "quota")
            if changes.get("require_approval") is False:
                require(principal, "disable", "require_approval")
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(TenantRow).where(TenantRow.id == tenant_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("tenant not found")
            if row.version != expected_version:
                raise ConflictError(f"tenant is at version {row.version}, not {expected_version}")
            for key, value in changes.items():
                setattr(row, key, value)
            row.version += 1
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="tenant.update",
                object_ref=f"tenant:{row.name}",
                diff=dict(changes),
                request_id=request_id,
            )
            return _record(
                row,
                (await _usage(session, [row.id]))[row.id],
                (await _members(session, [row.id])).get(row.id, 0),
            )

    async def delete(
        self, principal: Principal, tenant_id: UUID, *, request_id: str | None = None
    ) -> None:
        """Delete an empty tenant (system administrators only)."""
        require(principal, "manage", "tenant")
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(select(TenantRow).where(TenantRow.id == tenant_id))
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("tenant not found")
            for table in _OBJECT_TABLES:
                owned = (
                    await session.execute(
                        select(func.count()).select_from(table).where(table.tenant_id == tenant_id)
                    )
                ).scalar_one()
                if owned:
                    raise ConflictError("the tenant still owns objects; delete them first")
            await audit_log.record(
                session,
                tenant_id=None,
                actor=principal.actor,
                action="tenant.delete",
                object_ref=f"tenant:{row.name}",
                request_id=request_id,
            )
            await session.execute(delete(TenantRow).where(TenantRow.id == tenant_id))
