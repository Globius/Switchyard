"""Administration of identity providers and their group-to-role mappings (system administrators).

Used by ``syctl idp ...``. The API for this arrives with the admin screens; until then providers are
configured from the command line. A provider holds only a *reference* to its client secret.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, select

from switchyard.db import audit_log
from switchyard.db.auth_models import IdentityProviderRow, IdpRoleMappingRow
from switchyard.db.models import TenantRow
from switchyard.db.tenancy import admin_session
from switchyard.domain.validators import validate_name, validate_secret_ref
from switchyard.domain.workflow import Role
from switchyard.ids import uuid7
from switchyard.services.context import Databases
from switchyard.services.errors import AlreadyExistsError, NotFoundError


@dataclass(frozen=True)
class ProviderRecord:
    """An identity provider and its mappings."""

    id: UUID
    name: str
    issuer_url: str
    client_id: str
    enabled: bool
    mappings: tuple[tuple[str, str | None, Role], ...]  # (group, tenant name or None, role)


class IdentityProviderAdmin:
    """Create providers, map groups to roles, enable and disable."""

    def __init__(self, db: Databases) -> None:
        """Create the service."""
        self._db = db

    async def add_provider(
        self,
        name: str,
        issuer_url: str,
        client_id: str,
        *,
        client_secret_ref: str | None = None,
        scopes: str = "openid profile email",
        username_claim: str = "preferred_username",
        email_claim: str = "email",
        groups_claim: str = "groups",
    ) -> UUID:
        """Add an OIDC provider.

        Raises:
            ValueError: For a malformed name, URL or secret reference.
            AlreadyExistsError: If the name is taken.
        """
        validate_name(name)
        if not issuer_url.startswith(("https://", "http://localhost")):
            raise ValueError("issuer_url must be an https:// URL")
        if client_secret_ref is not None:
            validate_secret_ref(client_secret_ref)
        async with admin_session(self._db.admin) as session:
            existing = (
                await session.execute(
                    select(IdentityProviderRow.id).where(IdentityProviderRow.name == name)
                )
            ).first()
            if existing is not None:
                raise AlreadyExistsError(f"identity provider {name!r} already exists")
            provider_id = uuid7()
            session.add(
                IdentityProviderRow(
                    id=provider_id,
                    name=name,
                    issuer_url=issuer_url.rstrip("/"),
                    client_id=client_id,
                    client_secret_ref=client_secret_ref,
                    scopes=scopes,
                    username_claim=username_claim,
                    email_claim=email_claim,
                    groups_claim=groups_claim,
                )
            )
            await audit_log.record(
                session,
                tenant_id=None,
                actor="system:cli",
                action="idp.created",
                object_ref=f"idp:{name}",
            )
            return provider_id

    async def map_group(
        self, provider: str, group: str, role: Role, *, tenant: str | None = None
    ) -> None:
        """Grant ``role`` to a provider group in ``tenant`` (``system-admin`` takes no tenant)."""
        if (role is Role.SYSTEM_ADMIN) != (tenant is None):
            raise ValueError("system-admin takes no tenant; every other role needs one")
        async with admin_session(self._db.admin) as session:
            provider_id = (
                await session.execute(
                    select(IdentityProviderRow.id).where(IdentityProviderRow.name == provider)
                )
            ).scalar_one_or_none()
            if provider_id is None:
                raise NotFoundError(f"identity provider {provider!r} does not exist")
            tenant_id: UUID | None = None
            if tenant is not None:
                tenant_id = (
                    await session.execute(select(TenantRow.id).where(TenantRow.name == tenant))
                ).scalar_one_or_none()
                if tenant_id is None:
                    raise NotFoundError(f"tenant {tenant!r} does not exist")
            session.add(
                IdpRoleMappingRow(
                    id=uuid7(),
                    provider_id=provider_id,
                    group_name=group,
                    scope_tenant_id=tenant_id,
                    role=role.value,
                )
            )
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor="system:cli",
                action="idp.group_mapped",
                object_ref=f"idp:{provider}",
                diff={"group": group, "role": role.value},
            )

    async def set_enabled(self, provider: str, enabled: bool) -> None:
        """Enable or disable a provider (disabled providers vanish from the login page)."""
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(IdentityProviderRow).where(IdentityProviderRow.name == provider)
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"identity provider {provider!r} does not exist")
            row.enabled = enabled

    async def remove(self, provider: str) -> None:
        """Delete a provider, its mappings and any login in progress."""
        async with admin_session(self._db.admin) as session:
            result = await session.execute(
                delete(IdentityProviderRow).where(IdentityProviderRow.name == provider)
            )
            if result.rowcount == 0:  # type: ignore[attr-defined]
                raise NotFoundError(f"identity provider {provider!r} does not exist")

    async def list_providers(self) -> list[ProviderRecord]:
        """All providers with their mappings."""
        async with admin_session(self._db.admin) as session:
            providers = list(
                (
                    await session.execute(
                        select(IdentityProviderRow).order_by(IdentityProviderRow.name)
                    )
                ).scalars()
            )
            tenants = {t.id: t.name for t in (await session.execute(select(TenantRow))).scalars()}
            out: list[ProviderRecord] = []
            for p in providers:
                rows = (
                    await session.execute(
                        select(IdpRoleMappingRow)
                        .where(IdpRoleMappingRow.provider_id == p.id)
                        .order_by(IdpRoleMappingRow.group_name)
                    )
                ).scalars()
                out.append(
                    ProviderRecord(
                        p.id,
                        p.name,
                        p.issuer_url,
                        p.client_id,
                        p.enabled,
                        tuple(
                            (
                                m.group_name,
                                tenants.get(m.scope_tenant_id) if m.scope_tenant_id else None,
                                Role(m.role),
                            )
                            for m in rows
                        ),
                    )
                )
            return out
