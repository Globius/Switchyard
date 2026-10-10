"""Tenants: created and changed by system administrators (outside change sets).

Tenants are the root of ownership and are not part of any tenant's own state, so they are managed
directly with the admin database role and audited, not through change sets.
"""

import contextlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select

from switchyard.db import audit_log
from switchyard.db import models as m
from switchyard.db.models import TenantRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.validators import validate_name
from switchyard.domain.workflow import Role
from switchyard.ids import uuid7
from switchyard.services.changesets import require_role
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import (
    AlreadyExistsError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
)

_OBJECT_TABLES = (
    m.VirtualServiceRow,
    m.PoolRow,
    m.PolicySetRow,
    m.CertificateRow,
    m.HealthMonitorRow,
    m.VipPoolRow,
    m.SeGroupRow,
    m.NetworkRow,
)


@dataclass(frozen=True)
class TenantRecord:
    """A tenant."""

    id: UUID
    name: str
    version: int
    oidc_group: str | None
    quotas: dict[str, int]
    require_approval: bool


def _record(row: TenantRow) -> TenantRecord:
    return TenantRecord(
        id=row.id,
        name=row.name,
        version=row.version,
        oidc_group=row.oidc_group,
        quotas=dict(row.quotas),
        require_approval=row.require_approval,
    )


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
        if row is None or not principal.can_access(row.id):
            raise NotFoundError(f"tenant {ref!r} not found")
        return _record(row)

    async def list_tenants(self, principal: Principal) -> list[TenantRecord]:
        """List the tenants the principal can access."""
        async with admin_session(self._db.admin) as session:
            query = select(TenantRow).order_by(TenantRow.name)
            if not principal.is_system:
                query = query.where(TenantRow.id.in_(sorted(principal.tenant_ids)))
            return [_record(r) for r in (await session.execute(query)).scalars()]

    async def create(
        self,
        principal: Principal,
        name: str,
        *,
        oidc_group: str | None = None,
        quotas: dict[str, int] | None = None,
        require_approval: bool = False,
        request_id: str | None = None,
    ) -> TenantRecord:
        """Create a tenant (system administrators only)."""
        require_role(principal, Role.SYSTEM_ADMIN)
        validate_name(name)
        async with admin_session(self._db.admin) as session:
            if (await session.execute(select(TenantRow.id).where(TenantRow.name == name))).first():
                raise AlreadyExistsError(f"tenant {name!r} already exists")
            row = TenantRow(
                id=uuid7(),
                name=name,
                oidc_group=oidc_group,
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
        """Change ``oidc_group``, ``quotas`` or ``require_approval``.

        System administrators may change any of them; tenant administrators may only toggle
        ``require_approval`` for their own tenant (turning approval *on* is always allowed; turning
        it off needs a system administrator, so a compromised tenant account cannot drop review).
        """
        allowed = {"oidc_group", "quotas", "require_approval"}
        if unknown := set(changes) - allowed:
            raise ValueError(f"cannot change: {', '.join(sorted(unknown))}")
        if not principal.can_access(tenant_id):
            raise NotFoundError("tenant not found")
        if not principal.is_system:
            require_role(principal, Role.TENANT_ADMIN)
            if set(changes) - {"require_approval"} or changes.get("require_approval") is False:
                raise PermissionDeniedError("only a system administrator may make this change")
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
            return _record(row)

    async def delete(
        self, principal: Principal, tenant_id: UUID, *, request_id: str | None = None
    ) -> None:
        """Delete an empty tenant (system administrators only)."""
        require_role(principal, Role.SYSTEM_ADMIN)
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
