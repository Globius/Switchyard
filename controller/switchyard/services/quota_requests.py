"""Quota requests: a tenant asks for a higher limit and a system administrator decides (ADR 0199).

The tenant's side (file, list, withdraw) runs under the application role, so row-level security
keeps one tenant from seeing another's requests. The decision runs under the admin role because it
reads across tenants and changes the tenant's quotas in the same transaction, so a granted limit
and the request that granted it cannot disagree.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from switchyard.db import audit_log
from switchyard.db.models import TenantRow
from switchyard.db.portal_models import QuotaRequestRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.changes import STICK_TABLE_QUOTA, ObjectType
from switchyard.domain.quota_requests import QuotaRequestState, check_grant, check_request
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.errors import ConflictError, FieldErrors, NotFoundError


@dataclass(frozen=True)
class QuotaRequestView:
    """A quota request as the API shows it."""

    id: UUID
    tenant_id: UUID
    tenant: str
    resource: str
    current_limit: int
    requested_limit: int
    reason: str
    state: str
    requested_by: str
    requested_at: datetime
    granted_limit: int | None
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None


def _view(row: QuotaRequestRow, tenant: str) -> QuotaRequestView:
    return QuotaRequestView(
        id=row.id,
        tenant_id=row.tenant_id,
        tenant=tenant,
        resource=row.resource,
        current_limit=row.current_limit,
        requested_limit=row.requested_limit,
        reason=row.reason,
        state=row.state,
        requested_by=row.requested_by,
        requested_at=row.requested_at,
        granted_limit=row.granted_limit,
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        decision_note=row.decision_note,
    )


def _valid_resources() -> set[str]:
    return {t.value for t in ObjectType} | {STICK_TABLE_QUOTA}


class QuotaRequests:
    """File, list, withdraw and decide quota requests."""

    def __init__(self, db: Databases, clock: Clock = utcnow) -> None:
        """Create the service over the two database handles."""
        self._db = db
        self._clock = clock

    async def _tenant(self, tenant_id: UUID) -> TenantRow:
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(select(TenantRow).where(TenantRow.id == tenant_id))
            ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("tenant not found")
        return row

    async def list_for_tenant(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        state: QuotaRequestState | None = None,
        limit: int = 100,
    ) -> list[QuotaRequestView]:
        """The tenant's requests, newest first. Anyone who can read the tenant can read these."""
        if not principal.can_access(tenant_id):
            raise NotFoundError("tenant not found")
        name = (await self._tenant(tenant_id)).name
        async with tenant_session(self._db.app, [tenant_id]) as session:
            query = select(QuotaRequestRow).order_by(QuotaRequestRow.requested_at.desc())
            if state is not None:
                query = query.where(QuotaRequestRow.state == state.value)
            rows = (await session.execute(query.limit(limit))).scalars()
            return [_view(r, name) for r in rows]

    async def list_all(
        self,
        principal: Principal,
        *,
        state: QuotaRequestState | None = QuotaRequestState.PENDING,
        limit: int = 100,
    ) -> list[QuotaRequestView]:
        """Requests of every tenant, oldest first (the queue a system administrator works)."""
        require(principal, "decide", "quota_request")
        async with admin_session(self._db.admin) as session:
            query = select(QuotaRequestRow, TenantRow.name).join(
                TenantRow, TenantRow.id == QuotaRequestRow.tenant_id
            )
            if state is not None:
                query = query.where(QuotaRequestRow.state == state.value)
            rows = (
                await session.execute(query.order_by(QuotaRequestRow.requested_at).limit(limit))
            ).all()
            return [_view(r, name) for r, name in rows]

    async def create(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        resource: str,
        requested_limit: int,
        reason: str,
        request_id: str | None = None,
    ) -> QuotaRequestView:
        """File a request for a higher limit.

        Raises:
            FieldErrors: The resource is unknown, has no quota, the new limit is not higher, or the
                reason is missing (paths ``resource``, ``requested_limit``, ``reason``).
            ConflictError: The tenant already has a pending request for the resource.
        """
        if not principal.can_access(tenant_id):
            raise NotFoundError("tenant not found")
        require(principal, "request", "quota_request")
        tenant = await self._tenant(tenant_id)
        errors = FieldErrors()
        if resource not in _valid_resources():
            errors.add("resource", "invalid", f"unknown quota {resource!r}")
        else:
            for path, problem in check_request(
                resource, tenant.quotas.get(resource), requested_limit, reason
            ):
                errors.add(path, "invalid", problem)
        errors.raise_if_any()
        async with tenant_session(self._db.app, [tenant_id]) as session:
            pending = (
                await session.execute(
                    select(QuotaRequestRow.id).where(
                        QuotaRequestRow.resource == resource,
                        QuotaRequestRow.state == QuotaRequestState.PENDING.value,
                    )
                )
            ).first()
            if pending:
                raise ConflictError(
                    f"there is already a pending request for {resource}; withdraw it first"
                )
            row = QuotaRequestRow(
                id=uuid7(),
                tenant_id=tenant_id,
                resource=resource,
                current_limit=tenant.quotas[resource],
                requested_limit=requested_limit,
                reason=reason.strip(),
                state=QuotaRequestState.PENDING.value,
                requested_by=principal.actor,
                requested_at=self._clock(),
            )
            session.add(row)
            await session.flush()
            view = _view(row, tenant.name)
        await self._audit(
            principal,
            tenant_id,
            "quota_request.create",
            view,
            {"resource": resource, "from": view.current_limit, "to": requested_limit},
            request_id,
        )
        return view

    async def withdraw(
        self,
        principal: Principal,
        tenant_id: UUID,
        request: UUID,
        *,
        request_id: str | None = None,
    ) -> QuotaRequestView:
        """Take a pending request back. Anyone who may file requests may withdraw them."""
        if not principal.can_access(tenant_id):
            raise NotFoundError("tenant not found")
        require(principal, "request", "quota_request")
        name = (await self._tenant(tenant_id)).name
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = (
                await session.execute(
                    select(QuotaRequestRow).where(QuotaRequestRow.id == request).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("quota request not found")
            if row.state != QuotaRequestState.PENDING.value:
                raise ConflictError(f"the request is already {row.state}")
            row.state = QuotaRequestState.WITHDRAWN.value
            row.version += 1
            row.decided_by = principal.actor
            row.decided_at = self._clock()
            view = _view(row, name)
        await self._audit(principal, tenant_id, "quota_request.withdraw", view, {}, request_id)
        return view

    async def decide(
        self,
        principal: Principal,
        request: UUID,
        *,
        approve: bool,
        granted_limit: int | None = None,
        note: str | None = None,
        request_id: str | None = None,
    ) -> QuotaRequestView:
        """Approve (setting the tenant's quota) or deny a pending request.

        Args:
            principal: A system administrator.
            request: The request.
            approve: Grant it or refuse it. A denial needs a note.
            granted_limit: With ``approve``, the new limit; by default what was asked for. It lies
                between the tenant's current limit and the requested one.
            note: Shown to the tenant next to the decision.
            request_id: The API request, for the audit log.

        Raises:
            FieldErrors: A denial without a note, or a grant outside the allowed range.
            ConflictError: The request is not pending, or the tenant's quota changed meanwhile so
                that the grant no longer raises it.
        """
        require(principal, "decide", "quota_request")
        note = (note or "").strip() or None
        errors = FieldErrors()
        if not approve and note is None:
            errors.add("note", "required", "say why the request is denied")
        if note is not None and len(note) > 500:
            errors.add("note", "invalid", "the note must be at most 500 characters")
        errors.raise_if_any()
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(QuotaRequestRow).where(QuotaRequestRow.id == request).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError("quota request not found")
            if row.state != QuotaRequestState.PENDING.value:
                raise ConflictError(f"the request is already {row.state}")
            tenant = (
                await session.execute(
                    select(TenantRow).where(TenantRow.id == row.tenant_id).with_for_update()
                )
            ).scalar_one()
            diff: dict[str, Any] = {"resource": row.resource}
            if approve:
                current = tenant.quotas.get(row.resource)
                if current is None or current >= row.requested_limit:
                    raise ConflictError(
                        f"the tenant's quota for {row.resource} is now "
                        f"{'unlimited' if current is None else current}; there is nothing to grant"
                    )
                granted = row.requested_limit if granted_limit is None else granted_limit
                errors = FieldErrors()
                for problem in check_grant(current, row.requested_limit, granted):
                    errors.add("granted_limit", "invalid", problem)
                errors.raise_if_any()
                tenant.quotas = {**tenant.quotas, row.resource: granted}
                tenant.version += 1
                row.granted_limit = granted
                diff |= {"from": current, "to": granted}
            row.state = (QuotaRequestState.APPROVED if approve else QuotaRequestState.DENIED).value
            row.decided_by = principal.actor
            row.decided_at = self._clock()
            row.decision_note = note
            row.version += 1
            view = _view(row, tenant.name)
            await audit_log.record(
                session,
                tenant_id=row.tenant_id,
                actor=principal.actor,
                action="quota_request.approve" if approve else "quota_request.deny",
                object_ref=f"quota_request:{row.id}",
                diff=diff,
                request_id=request_id,
            )
        return view

    async def _audit(
        self,
        principal: Principal,
        tenant_id: UUID,
        action: str,
        view: QuotaRequestView,
        diff: dict[str, Any],
        request_id: str | None,
    ) -> None:
        async with admin_session(self._db.admin) as session:
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action=action,
                object_ref=f"quota_request:{view.id}",
                diff=diff or {"resource": view.resource},
                request_id=request_id,
            )
