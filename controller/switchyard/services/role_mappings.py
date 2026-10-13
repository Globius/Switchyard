# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Role mappings: which provider group grants which role (system-wide or in one tenant).

A mapping says "members of ``group_name`` at ``provider`` get ``role`` in ``tenant``"
(``system-admin`` takes no tenant). Mappings are the only way a group grants access (ADR 0030). A
system administrator manages all of them; a tenant administrator manages the ``viewer`` and
``operator`` mappings of their own tenant. Every change is audited. Editing a mapping changes
people's grants within a minute: sessions recompute from the stored groups (see
:mod:`switchyard.services.sessions`).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, select
from sqlalchemy.exc import IntegrityError

from switchyard.db import audit_log
from switchyard.db.auth_models import IdentityProviderRow, IdpRoleMappingRow
from switchyard.db.models import TenantRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.workflow import Role
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.errors import (
    AlreadyExistsError,
    NotFoundError,
    PreconditionFailedError,
)

#: Roles a tenant administrator may map in their own tenant.
TENANT_MANAGED_ROLES = frozenset({Role.VIEWER, Role.OPERATOR})
MAX_GROUP_LENGTH = 255


@dataclass(frozen=True)
class RoleMappingRecord:
    """A role mapping as the API shows it."""

    id: UUID
    provider_id: UUID
    provider: str
    group_name: str
    label: str | None
    tenant_id: UUID | None
    tenant: str | None
    role: Role
    version: int
    created_by: str
    created_at: datetime


def validate_group_name(value: str) -> str:
    """A provider group name: non-empty, at most 255 characters, no control characters.

    Group names are compared exactly and case-sensitively, so nothing is normalised.

    Example:
        >>> validate_group_name("acme-admins")
        'acme-admins'
        >>> validate_group_name("")
        Traceback (most recent call last):
            ...
        ValueError: group_name must be 1-255 printable characters
    """
    if not value or len(value) > MAX_GROUP_LENGTH or not value.isprintable():
        raise ValueError("group_name must be 1-255 printable characters")
    return value


def provider_clause(provider: str) -> ColumnElement[bool]:
    """Match an identity provider by id or, if ``provider`` is not a UUID, by name."""
    try:
        return IdentityProviderRow.id == UUID(provider)
    except ValueError:
        return IdentityProviderRow.name == provider


class RoleMappingService:
    """List, create, change and delete role mappings."""

    def __init__(self, db: Databases, clock: Clock = utcnow) -> None:
        """Create the service."""
        self._db = db
        self._clock = clock

    @staticmethod
    def _authorize(principal: Principal, tenant_id: UUID | None, role: Role) -> None:
        """Check that ``principal`` may manage a mapping of ``role`` in ``tenant_id``.

        Tenant administrators: ``viewer``/``operator`` mappings of their own tenant. Everything
        else needs a system administrator.
        """
        if tenant_id is not None and role in TENANT_MANAGED_ROLES:
            if not principal.can_access(tenant_id):
                raise NotFoundError("tenant not found")
            require(principal, "manage", "role_mapping")
        else:
            require(principal, "manage", "role_mapping_privileged")

    @staticmethod
    def _authorize_read(principal: Principal, tenant_id: UUID | None) -> None:
        if tenant_id is None:
            require(principal, "manage", "role_mapping_privileged")
        else:
            if not principal.can_access(tenant_id):
                raise NotFoundError("tenant not found")
            require(principal, "manage", "role_mapping")

    async def list(
        self,
        principal: Principal,
        *,
        tenant_id: UUID | None = None,
        provider: str | None = None,
        group: str | None = None,
        tenant_filter: UUID | None = None,
    ) -> list[RoleMappingRecord]:
        """List mappings.

        Without ``tenant_id`` this is the system-wide list (system administrators), which
        ``tenant_filter`` can narrow. With it, the mappings of that tenant (tenant administrators
        and up). ``provider`` is an id or a name.
        """
        self._authorize_read(principal, tenant_id)
        scope = tenant_id or tenant_filter
        async with admin_session(self._db.admin) as session:
            query = (
                select(IdpRoleMappingRow, IdentityProviderRow.name, TenantRow.name)
                .join(IdentityProviderRow, IdentityProviderRow.id == IdpRoleMappingRow.provider_id)
                .outerjoin(TenantRow, TenantRow.id == IdpRoleMappingRow.scope_tenant_id)
                .order_by(
                    IdentityProviderRow.name, IdpRoleMappingRow.group_name, IdpRoleMappingRow.id
                )
            )
            if scope is not None:
                query = query.where(IdpRoleMappingRow.scope_tenant_id == scope)
            if group is not None:
                query = query.where(IdpRoleMappingRow.group_name == group)
            if provider is not None:
                query = query.where(provider_clause(provider))
            return [_record(*r) for r in (await session.execute(query)).all()]

    async def get(
        self, principal: Principal, mapping_id: UUID, *, tenant_id: UUID | None = None
    ) -> RoleMappingRecord:
        """One mapping. On a tenant route it must belong to that tenant (else 404)."""
        self._authorize_read(principal, tenant_id)
        async with admin_session(self._db.admin) as session:
            return _record(*await self._load(session, mapping_id, tenant_id))

    async def create(
        self,
        principal: Principal,
        *,
        provider: str,
        group_name: str,
        role: Role,
        tenant_id: UUID | None,
        label: str | None = None,
        request_id: str | None = None,
    ) -> RoleMappingRecord:
        """Create a mapping.

        Raises:
            ValueError: For a bad group name, or a role/tenant combination that does not exist
                (``system-admin`` takes no tenant, every other role needs one).
            PermissionDeniedError: If the caller may not manage this kind of mapping.
            NotFoundError: If the provider or tenant does not exist.
            AlreadyExistsError: If (provider, group, tenant) is already mapped.
        """
        validate_group_name(group_name)
        if (role is Role.SYSTEM_ADMIN) != (tenant_id is None):
            raise ValueError("system-admin takes no tenant; every other role needs one")
        self._authorize(principal, tenant_id, role)
        async with admin_session(self._db.admin) as session:
            provider_row = (
                await session.execute(select(IdentityProviderRow).where(provider_clause(provider)))
            ).scalar_one_or_none()
            if provider_row is None:
                raise NotFoundError("identity provider not found")
            if tenant_id is not None and await session.get(TenantRow, tenant_id) is None:
                raise NotFoundError("tenant not found")
            scope = (
                IdpRoleMappingRow.scope_tenant_id.is_(None)
                if tenant_id is None
                else IdpRoleMappingRow.scope_tenant_id == tenant_id
            )
            duplicate = (
                await session.execute(
                    select(IdpRoleMappingRow.id).where(
                        IdpRoleMappingRow.provider_id == provider_row.id,
                        IdpRoleMappingRow.group_name == group_name,
                        scope,
                    )
                )
            ).first()
            if duplicate is not None:
                raise AlreadyExistsError(
                    f"group {group_name!r} of {provider_row.name!r} is already mapped there"
                )
            row = IdpRoleMappingRow(
                id=uuid7(),
                provider_id=provider_row.id,
                group_name=group_name,
                scope_tenant_id=tenant_id,
                role=role.value,
                label=label,
                version=1,
                created_by=principal.actor,
                created_at=self._clock(),
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError as exc:  # raced with another create
                raise AlreadyExistsError("this group is already mapped there") from exc
            await _audit(
                session, principal, "role_mapping.create", row, provider_row.name, request_id
            )
            tenant_name = None
            if tenant_id is not None:
                tenant_name = (
                    await session.execute(select(TenantRow.name).where(TenantRow.id == tenant_id))
                ).scalar_one_or_none()
            return _record(row, provider_row.name, tenant_name)

    async def update(
        self,
        principal: Principal,
        mapping_id: UUID,
        *,
        expected_version: int,
        changes: dict[str, Any],
        tenant_id: UUID | None = None,
        request_id: str | None = None,
    ) -> RoleMappingRecord:
        """Change ``group_name``, ``label`` and/or ``role`` (the tenant of a mapping is fixed).

        Raises:
            PreconditionFailedError: If ``expected_version`` is stale.
            PermissionDeniedError: If the caller may not manage the current or the new role.
        """
        self._authorize_read(principal, tenant_id)  # a weak caller learns nothing about the id
        if unknown := set(changes) - {"group_name", "label", "role"}:
            raise ValueError(f"cannot change: {', '.join(sorted(unknown))}")
        async with admin_session(self._db.admin) as session:
            row, provider_name, tenant_name = await self._load(
                session, mapping_id, tenant_id, for_update=True
            )
            current = Role(row.role)
            self._authorize(principal, row.scope_tenant_id, current)
            new_role = Role(str(changes["role"])) if changes.get("role") is not None else current
            if (new_role is Role.SYSTEM_ADMIN) != (row.scope_tenant_id is None):
                raise ValueError("role: system-admin takes no tenant; every other role needs one")
            self._authorize(principal, row.scope_tenant_id, new_role)
            if row.version != expected_version:
                raise PreconditionFailedError(
                    f"mapping is at version {row.version}, not {expected_version}"
                )
            before = {"group_name": row.group_name, "label": row.label, "role": row.role}
            if changes.get("group_name") is not None:
                row.group_name = validate_group_name(str(changes["group_name"]))
            if "label" in changes:
                row.label = None if changes["label"] is None else str(changes["label"])
            row.role = new_role.value
            row.version += 1
            try:
                await session.flush()
            except IntegrityError as exc:
                raise AlreadyExistsError("this group is already mapped there") from exc
            await _audit(
                session, principal, "role_mapping.update", row, provider_name, request_id, before
            )
            return _record(row, provider_name, tenant_name)

    async def delete(
        self,
        principal: Principal,
        mapping_id: UUID,
        *,
        tenant_id: UUID | None = None,
        request_id: str | None = None,
    ) -> None:
        """Delete a mapping; the people it granted lose access within a minute."""
        self._authorize_read(principal, tenant_id)
        async with admin_session(self._db.admin) as session:
            row, provider_name, _ = await self._load(session, mapping_id, tenant_id)
            self._authorize(principal, row.scope_tenant_id, Role(row.role))
            await session.delete(row)
            await _audit(session, principal, "role_mapping.delete", row, provider_name, request_id)

    @staticmethod
    async def _load(
        session: Any, mapping_id: UUID, tenant_id: UUID | None, *, for_update: bool = False
    ) -> tuple[IdpRoleMappingRow, str, str | None]:
        query = (
            select(IdpRoleMappingRow, IdentityProviderRow.name, TenantRow.name)
            .join(IdentityProviderRow, IdentityProviderRow.id == IdpRoleMappingRow.provider_id)
            .outerjoin(TenantRow, TenantRow.id == IdpRoleMappingRow.scope_tenant_id)
            .where(IdpRoleMappingRow.id == mapping_id)
        )
        if tenant_id is not None:
            query = query.where(IdpRoleMappingRow.scope_tenant_id == tenant_id)
        if for_update:
            query = query.with_for_update(of=IdpRoleMappingRow)
        found = (await session.execute(query)).first()
        if found is None:
            raise NotFoundError("role mapping not found")
        return found[0], found[1], found[2]


async def _audit(
    session: Any,
    principal: Principal,
    action: str,
    row: IdpRoleMappingRow,
    provider_name: str,
    request_id: str | None,
    before: dict[str, Any] | None = None,
) -> None:
    diff: dict[str, Any] = {"provider": provider_name, "group": row.group_name, "role": row.role}
    if before is not None:
        diff["before"] = before
    await audit_log.record(
        session,
        tenant_id=row.scope_tenant_id,
        actor=principal.actor,
        action=action,
        object_ref=f"role_mapping:{row.id}",
        diff=diff,
        request_id=request_id,
    )


def _record(
    row: IdpRoleMappingRow, provider_name: str, tenant_name: str | None
) -> RoleMappingRecord:
    return RoleMappingRecord(
        id=row.id,
        provider_id=row.provider_id,
        provider=provider_name,
        group_name=row.group_name,
        label=row.label,
        tenant_id=row.scope_tenant_id,
        tenant=tenant_name,
        role=Role(row.role),
        version=row.version,
        created_by=row.created_by,
        created_at=row.created_at,
    )


__all__ = ["RoleMappingRecord", "RoleMappingService", "provider_clause", "validate_group_name"]
