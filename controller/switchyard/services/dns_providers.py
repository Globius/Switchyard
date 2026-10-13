# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""DNS credentials: where a tenant's DNS-01 records are published, and with which login (ADR 0079).

A tenant administrator adds a credential for a provider type (Cloudflare, Route 53, Azure DNS,
RFC 2136, PowerDNS). Each type declares its own form (``adapters.dns.base.CATALOGUE``), so one
generic screen serves them all. Secret fields are written to the secrets backend as one JSON
document and never returned; the row holds the non-secret settings, the zones the credential serves
and the reference.

A certificate name is published through the credential named on the certificate, or else through
the enabled credential with the longest matching zone, or else through the only credential that has
no zone list.
"""

import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from switchyard.adapters.dns.base import (
    DnsError,
    DnsProvider,
    ProviderType,
    check_settings,
    types,
)
from switchyard.adapters.dns.factory import build_provider
from switchyard.adapters.dns.zones import in_zone
from switchyard.adapters.secrets import SecretBackendError
from switchyard.db import audit_log
from switchyard.db.acme_models import DnsProviderRow
from switchyard.db.models import CertificateRow, TenantRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import validators as v
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import (
    AlreadyExistsError,
    ConflictError,
    FieldErrors,
    NotFoundError,
    PermissionDeniedError,
    PreconditionFailedError,
)

_ZONE_LABEL = "zones"


@dataclass(frozen=True)
class DnsProviderView:
    """A DNS credential as the API shows it (never any secret)."""

    id: UUID
    name: str
    type: str
    zones: list[str]
    config: dict[str, Any]
    credentials_set: bool
    enabled: bool
    version: int


@dataclass(frozen=True)
class DnsZoneResult:
    """Outcome of the test in one zone."""

    zone: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DnsTestResult:
    """Outcome of publishing and removing a test record in each zone of a credential."""

    ok: bool
    detail: str
    zones: list[DnsZoneResult]


@dataclass(frozen=True)
class Candidate:
    """A credential that could publish records for a name."""

    id: UUID
    name: str
    type: str
    zone: str | None


@dataclass(frozen=True)
class NameMatch:
    """The credentials that cover one name, best first (the longest zone, then catch-alls)."""

    name: str
    candidates: list[Candidate]


def _view(row: DnsProviderRow) -> DnsProviderView:
    return DnsProviderView(
        id=row.id,
        name=row.name,
        type=row.type,
        zones=list(row.zones),
        config=dict(row.config),
        credentials_set=bool(row.secret_ref),
        enabled=row.enabled,
        version=row.version,
    )


def _zones(raw: list[str], errors: FieldErrors) -> list[str]:
    out: list[str] = []
    for index, zone in enumerate(raw):
        clean = zone.strip().strip(".").lower()
        try:
            v.validate_hostname(clean)
        except ValueError as exc:
            errors.add(f"{_ZONE_LABEL}[{index}]", "invalid", str(exc))
            continue
        if clean not in out:
            out.append(clean)
    return out


def rank(rows: list[DnsProviderRow], domain: str) -> list[tuple[DnsProviderRow, str | None]]:
    """The credentials that can serve ``domain``, best first, with the zone that matched.

    A wildcard name is matched by its base domain. Credentials without zones come last.
    """
    base = domain.removeprefix("*.")
    hits: list[tuple[int, str, DnsProviderRow, str | None]] = []
    for row in rows:
        zones = [z for z in row.zones if in_zone(base, z)]
        if zones:
            best = max(zones, key=len)
            hits.append((-len(best), row.name, row, best))
        elif not row.zones:
            hits.append((1, row.name, row, None))
    return [(row, zone) for _, _, row, zone in sorted(hits, key=lambda h: (h[0], h[1]))]


class DnsResolver:
    """Finds the DNS provider for a name and closes what it opened."""

    def __init__(
        self,
        service: "DnsProviders",
        tenant_id: UUID,
        pinned: str | None,
        fallback: DnsProvider | None = None,
    ) -> None:
        """Create a resolver for one issuance.

        ``fallback`` serves tenants that have no credential at all (the deployment-wide Cloudflare
        token of earlier versions).
        """
        self._service = service
        self._tenant_id = tenant_id
        self._pinned = pinned
        self._fallback = fallback
        self._open: dict[UUID, DnsProvider] = {}
        self._rows: list[DnsProviderRow] | None = None

    async def resolve(self, domain: str) -> DnsProvider:
        """The provider that publishes records for ``domain``.

        Raises:
            DnsError: If no credential serves the name.
        """
        if self._rows is None:
            self._rows = await self._service.enabled_rows(self._tenant_id)
        if not self._rows and self._fallback is not None and not self._pinned:
            return self._fallback
        row = self._choose(domain)
        if row.id not in self._open:
            self._open[row.id] = await self._service.build(row)
        return self._open[row.id]

    def _choose(self, domain: str) -> DnsProviderRow:
        rows = self._rows or []
        if self._pinned:
            for row in rows:
                if row.name == self._pinned:
                    return row
            raise DnsError(f"DNS credential {self._pinned} does not exist or is disabled")
        ranked = rank(rows, domain)
        if ranked:
            return ranked[0][0]
        raise DnsError(
            f"no DNS credential serves {domain}: add one whose zones include it, "
            "or name the credential on the certificate"
        )

    async def close(self) -> None:
        """Close every provider this resolver opened."""
        for provider in self._open.values():
            await provider.close()
        self._open.clear()


class DnsProviders:
    """Manage a tenant's DNS credentials."""

    def __init__(self, db: Databases, secrets: Any, *, transport: Any = None) -> None:
        """Create the service.

        Args:
            db: Database access.
            secrets: A backend that reads and writes (OpenBao).
            transport: Replaceable HTTP transport for the providers (tests).
        """
        self._db = db
        self._secrets = secrets
        self._transport = transport

    @staticmethod
    def catalogue() -> list[ProviderType]:
        """The provider types and their forms."""
        return types()

    @staticmethod
    def _check_tenant(principal: Principal, tenant_id: UUID) -> None:
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")

    async def _tenant_name(self, tenant_id: UUID) -> str:
        async with admin_session(self._db.admin) as session:
            name = (
                await session.execute(select(TenantRow.name).where(TenantRow.id == tenant_id))
            ).scalar_one_or_none()
        if name is None:
            raise NotFoundError(f"tenant {tenant_id} does not exist")
        return str(name)

    # -- reading ----------------------------------------------------------------------------------

    async def list_providers(self, principal: Principal, tenant_id: UUID) -> list[DnsProviderView]:
        """The tenant's credentials by name."""
        self._check_tenant(principal, tenant_id)
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = (
                (await session.execute(select(DnsProviderRow).order_by(DnsProviderRow.name)))
                .scalars()
                .all()
            )
        return [_view(r) for r in rows]

    async def get(
        self, principal: Principal, tenant_id: UUID, provider_id: UUID
    ) -> DnsProviderView:
        """One credential."""
        self._check_tenant(principal, tenant_id)
        require(principal, "read", "tenant_data")
        return _view(await self._row(tenant_id, provider_id))

    async def _row(self, tenant_id: UUID, provider_id: UUID) -> DnsProviderRow:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await session.get(DnsProviderRow, provider_id)
        if row is None:
            raise NotFoundError(f"DNS credential {provider_id} does not exist")
        return row

    # -- writing ----------------------------------------------------------------------------------

    async def create(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        name: str,
        type_: str,
        zones: list[str],
        config: dict[str, Any],
        credentials: dict[str, Any],
        enabled: bool = True,
        request_id: str | None = None,
    ) -> DnsProviderView:
        """Add a credential and store its secrets.

        Raises:
            FieldErrors: With the path of every invalid field.
            AlreadyExistsError: If the name is taken in this tenant.
        """
        self._check_tenant(principal, tenant_id)
        require(principal, "manage", "dns_provider")
        errors = FieldErrors()
        try:
            v.validate_name(name)
        except ValueError as exc:
            errors.add("name", "invalid", str(exc))
        for path, message in check_settings(type_, config, credentials).items():
            errors.add(path, "invalid", message)
        clean_zones = _zones(zones, errors)
        await self._zone_conflicts(tenant_id, clean_zones, None, errors)
        errors.raise_if_any()
        tenant_name = await self._tenant_name(tenant_id)
        ref = f"kv/tenants/{tenant_name}/dns/{name}"
        try:
            await self._secrets.put(ref, json.dumps(credentials, sort_keys=True))
        except SecretBackendError as exc:
            raise ConflictError(f"could not store the credentials: {exc}") from exc
        async with tenant_session(self._db.app, [tenant_id]) as session:
            if (
                await session.execute(select(DnsProviderRow.id).where(DnsProviderRow.name == name))
            ).first():
                raise AlreadyExistsError(f"DNS credential {name} already exists")
            row = DnsProviderRow(
                id=uuid7(),
                tenant_id=tenant_id,
                name=name,
                type=type_,
                zones=clean_zones,
                config={k: v_ for k, v_ in config.items() if v_ not in (None, "")},
                secret_ref=ref,
                enabled=enabled,
            )
            session.add(row)
            await session.flush()
            view = _view(row)
        await self._audit(
            principal, tenant_id, "dns_provider.created", name, {"type": type_}, request_id
        )
        return view

    async def update(
        self,
        principal: Principal,
        tenant_id: UUID,
        provider_id: UUID,
        *,
        expected_version: int | None = None,
        zones: list[str] | None = None,
        config: dict[str, Any] | None = None,
        credentials: dict[str, Any] | None = None,
        enabled: bool | None = None,
        request_id: str | None = None,
    ) -> DnsProviderView:
        """Change a credential; omitted settings and omitted secrets stay as they are.

        Credentials sent here replace the stored secret fields that they name and keep the others.
        """
        self._check_tenant(principal, tenant_id)
        require(principal, "manage", "dns_provider")
        current = await self._row(tenant_id, provider_id)
        self._check_version(current, expected_version)
        merged = {**current.config, **(config or {})}
        merged = {k: v_ for k, v_ in merged.items() if v_ not in (None, "")}
        errors = FieldErrors()
        for path, message in check_settings(
            current.type, merged, credentials or {}, secrets_required=False
        ).items():
            errors.add(path, "invalid", message)
        clean_zones = _zones(zones, errors) if zones is not None else None
        if clean_zones is not None:
            await self._zone_conflicts(tenant_id, clean_zones, provider_id, errors)
        errors.raise_if_any()
        if credentials:
            try:
                stored = json.loads(await self._secrets.get(current.secret_ref))
                await self._secrets.put(
                    current.secret_ref, json.dumps({**stored, **credentials}, sort_keys=True)
                )
            except SecretBackendError as exc:
                raise ConflictError(f"could not store the credentials: {exc}") from exc
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await session.get(DnsProviderRow, provider_id, with_for_update=True)
            if row is None:
                raise NotFoundError(f"DNS credential {provider_id} does not exist")
            self._check_version(row, expected_version)
            if config is not None:
                row.config = merged
            if clean_zones is not None:
                row.zones = clean_zones
            if enabled is not None:
                row.enabled = enabled
            row.version += 1
            await session.flush()
            view = _view(row)
        await self._audit(
            principal,
            tenant_id,
            "dns_provider.updated",
            current.name,
            {"credentials_changed": bool(credentials)},
            request_id,
        )
        return view

    async def delete(
        self,
        principal: Principal,
        tenant_id: UUID,
        provider_id: UUID,
        *,
        expected_version: int | None = None,
        request_id: str | None = None,
    ) -> None:
        """Remove a credential that no certificate names."""
        self._check_tenant(principal, tenant_id)
        require(principal, "manage", "dns_provider")
        current = await self._row(tenant_id, provider_id)
        self._check_version(current, expected_version)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            used = (
                await session.execute(
                    select(func.count())
                    .select_from(CertificateRow)
                    .where(CertificateRow.dns_provider == current.name)
                )
            ).scalar_one()
            if used:
                raise ConflictError(
                    f"{used} certificate(s) name the DNS credential {current.name}; "
                    "change them first"
                )
            row = await session.get(DnsProviderRow, provider_id)
            if row is not None:
                await session.delete(row)
        await self._audit(
            principal, tenant_id, "dns_provider.deleted", current.name, {}, request_id
        )

    async def _zone_conflicts(
        self, tenant_id: UUID, zones: list[str], own: UUID | None, errors: FieldErrors
    ) -> None:
        """Two credentials of a tenant cannot claim the same zone."""
        async with tenant_session(self._db.app, [tenant_id]) as session:
            others = (
                (await session.execute(select(DnsProviderRow).where(DnsProviderRow.id != own)))
                .scalars()
                .all()
                if own
                else (await session.execute(select(DnsProviderRow))).scalars().all()
            )
        claimed = {zone: row.name for row in others for zone in row.zones}
        for index, zone in enumerate(zones):
            if zone in claimed:
                errors.add(
                    f"{_ZONE_LABEL}[{index}]",
                    "conflict",
                    f"already served by the credential {claimed[zone]}",
                )

    async def match(
        self, principal: Principal, tenant_id: UUID, names: list[str]
    ) -> list[NameMatch]:
        """For each name, the enabled credentials that cover it, best first."""
        self._check_tenant(principal, tenant_id)
        require(principal, "read", "tenant_data")
        rows = await self.enabled_rows(tenant_id)
        return [
            NameMatch(
                name,
                [Candidate(r.id, r.name, r.type, zone) for r, zone in rank(rows, name)],
            )
            for name in names
        ]

    @staticmethod
    def _check_version(row: DnsProviderRow, expected: int | None) -> None:
        if expected is not None and row.version != expected:
            raise PreconditionFailedError(
                f"DNS credential {row.name} is at version {row.version}, not {expected}"
            )

    async def _audit(
        self,
        principal: Principal,
        tenant_id: UUID,
        action: str,
        name: str,
        diff: dict[str, Any],
        request_id: str | None,
    ) -> None:
        async with admin_session(self._db.admin) as session:
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action=action,
                object_ref=f"dns_provider:{name}",
                diff=diff,
                request_id=request_id,
            )

    # -- using ------------------------------------------------------------------------------------

    async def enabled_rows(self, tenant_id: UUID) -> list[DnsProviderRow]:
        """The tenant's enabled credentials (no permission check: the issuer calls this)."""
        async with tenant_session(self._db.app, [tenant_id]) as session:
            return list(
                (
                    await session.execute(
                        select(DnsProviderRow)
                        .where(DnsProviderRow.enabled)
                        .order_by(DnsProviderRow.name)
                    )
                )
                .scalars()
                .all()
            )

    async def build(self, row: DnsProviderRow) -> DnsProvider:
        """Create the client for a stored credential."""
        try:
            credentials = json.loads(await self._secrets.get(row.secret_ref))
        except Exception as exc:
            raise DnsError(f"cannot read the secrets of DNS credential {row.name}: {exc}") from exc
        return build_provider(row.type, row.config, credentials, transport=self._transport)

    def resolver(
        self, tenant_id: UUID, pinned: str | None = None, fallback: DnsProvider | None = None
    ) -> DnsResolver:
        """A resolver for one issuance; close it when done."""
        return DnsResolver(self, tenant_id, pinned, fallback)

    async def test(
        self,
        principal: Principal,
        tenant_id: UUID,
        provider_id: UUID,
        *,
        zone: str | None = None,
        request_id: str | None = None,
    ) -> DnsTestResult:
        """Publish and remove a TXT record under ``_switchyard-test.<zone>``.

        The zone is the one given, the credential's configured zone, or its first listed zone.
        """
        self._check_tenant(principal, tenant_id)
        require(principal, "manage", "dns_provider")
        row = await self._row(tenant_id, provider_id)
        chosen = (
            [zone]
            if zone
            else list(row.zones) or ([row.config["zone"]] if row.config.get("zone") else [])
        )
        if not chosen:
            return DnsTestResult(False, "give a zone to test in (the credential lists none)", [])
        results: list[DnsZoneResult] = []
        provider: DnsProvider | None = None
        try:
            provider = await self.build(row)
            for name_zone in chosen:
                name = f"_switchyard-test.{name_zone}"
                try:
                    handle = await provider.create_txt(name, "switchyard-test", ttl=60)
                    await provider.delete_txt(name, handle)
                    results.append(DnsZoneResult(name_zone, True, f"published and removed {name}"))
                except (DnsError, ValueError) as exc:
                    results.append(DnsZoneResult(name_zone, False, str(exc)))
        except (DnsError, ValueError) as exc:
            results = [DnsZoneResult(z, False, str(exc)) for z in chosen]
        finally:
            if provider is not None:
                await provider.close()
        failed = [r for r in results if not r.ok]
        result = DnsTestResult(
            not failed,
            "every zone works"
            if not failed
            else f"{len(failed)} of {len(results)} zones failed: {failed[0].detail}",
            results,
        )
        await self._audit(
            principal, tenant_id, "dns_provider.tested", row.name, {"ok": result.ok}, request_id
        )
        return result
