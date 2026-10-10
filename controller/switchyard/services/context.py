"""Shared types for the service layer: who is acting, and which databases they use."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncEngine

from switchyard.domain.workflow import Role

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class Databases:
    """The two database handles every service needs.

    Attributes:
        app: Engine connected as ``switchyard_app`` (subject to RLS). Tenant work uses this one.
        admin: Engine connected as ``switchyard_admin`` (bypasses RLS). Used for token lookup,
            the job queue and cross-tenant jobs.
    """

    app: AsyncEngine
    admin: AsyncEngine

    async def dispose(self) -> None:
        """Close both pools."""
        await self.app.dispose()
        await self.admin.dispose()


@dataclass(frozen=True)
class Principal:
    """Who is acting. Built by the authentication layer, trusted by the services.

    Attributes:
        actor: Stable label for audit events, for example ``token:ci-bot``.
        role: The highest role held.
        tenant_ids: Tenants the actor may act in. Empty for a system administrator, who may act in
            every tenant.
    """

    actor: str
    role: Role
    tenant_ids: frozenset[UUID] = frozenset()

    @property
    def is_system(self) -> bool:
        """True for system administrators."""
        return self.role is Role.SYSTEM_ADMIN

    def can_access(self, tenant_id: UUID) -> bool:
        """True if the principal may act inside ``tenant_id``."""
        return self.is_system or tenant_id in self.tenant_ids
