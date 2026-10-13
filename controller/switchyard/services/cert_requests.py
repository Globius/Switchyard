# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Checking a certificate request, and the renewal history of certificates (ADR 0079).

``check`` answers what the request sheet needs before anything is queued: is the ACME provider
usable, does every name have a DNS credential that covers it (DNS-01), does the name lead to a
virtual service on port 80 (HTTP-01). The same findings refuse the request with ``422`` when it is
sent anyway.

Every issuance or renewal attempt is recorded (``certificate_renewal``) so the certificate list can
show the last status and the history sheet can show what happened and why.
"""

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select

from switchyard.db.acme_models import AcmeProviderRow, CertificateRenewalRow, DnsProviderRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import models as dm
from switchyard.domain import validators as v
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.dns_providers import rank
from switchyard.services.errors import FieldErrors, NotFoundError, PermissionDeniedError
from switchyard.services.tlsalpn import tls_services

MAX_NAMES = 50


@dataclass(frozen=True)
class IssueRequest:
    """What a person asks for when requesting a certificate.

    Attributes:
        name: Name of the certificate object.
        source: ``acme`` or ``cloudflare_origin``.
        sans: The names.
        acme_provider: Provider name (``acme``); the default provider when ``None``.
        challenge: ``dns-01`` or ``http-01``; ``dns-01`` when ``None``.
        dns_provider: Pinned DNS credential (``dns-01``); chosen by zone when ``None``.
        renew_policy: ``auto`` or ``manual``.
        renew_before_days: How long before expiry an automatic renewal starts.
    """

    name: str
    source: dm.CertSource
    sans: tuple[str, ...]
    acme_provider: str | None = None
    challenge: dm.AcmeChallenge | None = None
    dns_provider: str | None = None
    renew_policy: dm.RenewPolicy = dm.RenewPolicy.AUTO
    renew_before_days: int | None = None


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a request."""

    path: str
    message: str


@dataclass(frozen=True)
class CheckResult:
    """The findings of a request check."""

    problems: list[Problem]
    warnings: list[Problem]


@dataclass(frozen=True)
class RenewalRecord:
    """One issuance or renewal attempt."""

    id: UUID
    certificate: str
    trigger: str
    started_at: datetime
    finished_at: datetime
    ok: bool
    error: str | None
    change_set_id: UUID | None
    not_after: datetime | None
    acme_provider: str | None
    challenge: str | None


@dataclass(frozen=True)
class RenewalStatus:
    """The latest state of one certificate's renewals."""

    certificate_id: UUID
    certificate: str
    source: str
    issuer: str
    not_after: datetime
    renew_policy: str
    renew_before_days: int
    renew_due_at: datetime
    last_attempt_at: datetime | None
    last_ok: bool | None
    last_error: str | None
    last_success_at: datetime | None
    failures_in_a_row: int


def _record(row: CertificateRenewalRow) -> RenewalRecord:
    return RenewalRecord(
        id=row.id,
        certificate=row.certificate,
        trigger=row.trigger,
        started_at=row.started_at,
        finished_at=row.finished_at,
        ok=row.ok,
        error=row.error,
        change_set_id=row.change_set_id,
        not_after=row.not_after,
        acme_provider=row.acme_provider,
        challenge=row.challenge,
    )


async def _addresses(host: str) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """What ``host`` resolves to (empty when it does not)."""
    try:
        found = await asyncio.get_running_loop().getaddrinfo(host, 80, type=socket.SOCK_STREAM)
    except OSError:
        return set()
    return {ipaddress.ip_address(item[4][0]) for item in found}


class CertificateRequests:
    """Check certificate requests and read renewal history."""

    def __init__(self, db: Databases) -> None:
        """Create the service."""
        self._db = db

    @staticmethod
    def _check_tenant(principal: Principal, tenant_id: UUID) -> None:
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")

    # -- checking ---------------------------------------------------------------------------------

    async def check(
        self, principal: Principal, tenant_id: UUID, request: IssueRequest
    ) -> list[Problem]:
        """Everything wrong with ``request``, with the field path of each finding."""
        return (await self.inspect(principal, tenant_id, request)).problems

    async def inspect(
        self, principal: Principal, tenant_id: UUID, request: IssueRequest
    ) -> CheckResult:
        """Problems that stop the request and warnings that do not."""
        self._check_tenant(principal, tenant_id)
        require(principal, "manage", "certificate")
        problems: list[Problem] = []
        warnings: list[Problem] = []

        def add(path: str, message: str) -> None:
            problems.append(Problem(path, message))

        def warn(path: str, message: str) -> None:
            warnings.append(Problem(path, message))

        try:
            v.validate_name(request.name)
        except ValueError as exc:
            add("name", str(exc))
        if not 1 <= len(request.sans) <= MAX_NAMES:
            add("sans", f"give between 1 and {MAX_NAMES} names")
        if len(set(request.sans)) != len(request.sans):
            add("sans", "names must be unique")
        for index, san in enumerate(request.sans):
            try:
                v.validate_host_pattern(san)
            except ValueError as exc:
                add(f"sans[{index}]", str(exc))
        if request.source is dm.CertSource.ACME:
            await self._check_acme(tenant_id, request, add, warn)
        elif request.source is dm.CertSource.CLOUDFLARE_ORIGIN:
            if request.acme_provider or request.challenge or request.dns_provider:
                add("acme_provider", "only applies to ACME certificates")
        else:
            add("source", "can only be acme or cloudflare_origin")
        return CheckResult(problems, warnings)

    async def _check_acme(
        self, tenant_id: UUID, request: IssueRequest, add: Any, warn: Any
    ) -> None:
        async with admin_session(self._db.admin) as session:
            query = select(AcmeProviderRow)
            query = (
                query.where(AcmeProviderRow.name == request.acme_provider)
                if request.acme_provider
                else query.where(AcmeProviderRow.is_default)
            )
            provider = (await session.execute(query)).scalar_one_or_none()
        if (
            provider is None
            and not request.acme_provider
            and os.environ.get("SWITCHYARD_ACME_DIRECTORY")
        ):
            pass  # the provider of earlier versions is imported on first use
        elif provider is None:
            add(
                "acme_provider",
                f"ACME provider {request.acme_provider} does not exist"
                if request.acme_provider
                else "no default ACME provider is set: choose one",
            )
        elif not provider.enabled:
            add("acme_provider", f"ACME provider {provider.name} is disabled")
        challenge = request.challenge or dm.AcmeChallenge.DNS01
        if challenge is dm.AcmeChallenge.TLSALPN01:
            await self._check_tls_alpn(tenant_id, request, add, warn)
        elif challenge is dm.AcmeChallenge.HTTP01:
            await self._check_http01(tenant_id, request, add)
        else:
            await self._check_dns01(tenant_id, request, add)

    async def _check_dns01(self, tenant_id: UUID, request: IssueRequest, add: Any) -> None:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = list(
                (await session.execute(select(DnsProviderRow).where(DnsProviderRow.enabled)))
                .scalars()
                .all()
            )
        if request.dns_provider:
            pinned = next((r for r in rows if r.name == request.dns_provider), None)
            if pinned is None:
                add("dns_provider", f"DNS credential {request.dns_provider} does not exist")
                return
            for index, san in enumerate(request.sans):
                if pinned.zones and not any(row is pinned for row, _ in rank([pinned], san)):
                    add(f"sans[{index}]", f"not covered by the zones of {pinned.name}")
            return
        legacy = not rows and bool(os.environ.get("SWITCHYARD_CLOUDFLARE_TOKEN"))
        for index, san in enumerate(request.sans):
            if not rank(rows, san) and not legacy:
                add(f"sans[{index}]", "no DNS credential covers this name")

    async def _check_http01(self, tenant_id: UUID, request: IssueRequest, add: Any) -> None:
        if request.dns_provider:
            add("dns_provider", "only applies to the dns-01 challenge")
        if len(request.sans) != 1:
            add("sans", "HTTP-01 validates one name; use DNS-01 for several")
        for index, san in enumerate(request.sans):
            if san.startswith("*."):
                add(f"sans[{index}]", "wildcard names need DNS-01")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
        plain = [
            s
            for s in snapshot.virtual_services
            if s.enabled
            and s.protocol is dm.Protocol.HTTP
            and any(p.port == 80 and not p.tls for p in s.ports)
        ]
        vips = {s.vip for s in plain}
        open_vips = {s.vip for s in plain if not (s.allow_list_id and s.enforce_allow_list)}
        if not vips:
            add("sans[0]", "needs a virtual service with an HTTP port 80 listener")
            return
        for index, san in enumerate(request.sans):
            if san.startswith("*."):
                continue
            found = await _addresses(san)
            if not found:
                add(f"sans[{index}]", "does not resolve from the controller")
            elif not found & vips:
                add(
                    f"sans[{index}]",
                    "resolves to "
                    + ", ".join(sorted(str(a) for a in found))
                    + ", which is not a VIP of one of your HTTP services on port 80",
                )
            elif not found & open_vips:
                add(
                    f"sans[{index}]",
                    "its service rejects clients outside its allow list, so the CA cannot "
                    "reach it; use DNS-01",
                )

    async def _check_tls_alpn(
        self, tenant_id: UUID, request: IssueRequest, add: Any, warn: Any
    ) -> None:
        if request.dns_provider:
            add("dns_provider", "only applies to the dns-01 challenge")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
        services = tls_services(snapshot)
        vips = {s.vip for s in services}
        if not vips:
            add("sans[0]", "needs a virtual service with an HTTP TLS listener on port 443")
            return
        certs = {c.id: c for c in snapshot.certificates}
        for index, san in enumerate(request.sans):
            if san.startswith("*."):
                add(f"sans[{index}]", "wildcard names need DNS-01")
                continue
            found = await _addresses(san)
            if not found:
                add(f"sans[{index}]", "does not resolve from the controller")
            elif not found & vips:
                add(
                    f"sans[{index}]",
                    "resolves to "
                    + ", ".join(sorted(str(a) for a in found))
                    + ", which is not a VIP of one of your HTTPS services on port 443",
                )
            live = [
                s.name
                for s in services
                if any(
                    v.host_covered_by_san(san, name)
                    for cid in s.certificate_ids
                    if cid in certs
                    for name in certs[cid].sans
                )
            ]
            if live:
                warn(
                    f"sans[{index}]",
                    f"{san} is served over HTTPS by {', '.join(sorted(live))}: its visitors get a "
                    "certificate error for the few seconds validation takes",
                )

    async def validate(self, principal: Principal, tenant_id: UUID, request: IssueRequest) -> None:
        """Raise :class:`FieldErrors` with every finding of :meth:`check`."""
        errors = FieldErrors()
        for problem in await self.check(principal, tenant_id, request):
            errors.add(problem.path, "invalid", problem.message)
        errors.raise_if_any()

    # -- history ----------------------------------------------------------------------------------

    async def renewals(
        self, principal: Principal, tenant_id: UUID, certificate_id: UUID, *, limit: int = 50
    ) -> list[RenewalRecord]:
        """Attempts for one certificate, newest first."""
        self._check_tenant(principal, tenant_id)
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
            cert = next((c for c in snapshot.certificates if c.id == certificate_id), None)
            if cert is None:
                raise NotFoundError(f"certificate {certificate_id} does not exist")
            rows = (
                (
                    await session.execute(
                        select(CertificateRenewalRow)
                        .where(CertificateRenewalRow.certificate == cert.name)
                        .order_by(CertificateRenewalRow.started_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return [_record(r) for r in rows]

    async def status(self, principal: Principal, tenant_id: UUID) -> list[RenewalStatus]:
        """For every server certificate: when it renews and how the last attempts went."""
        self._check_tenant(principal, tenant_id)
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
            rows = (
                (
                    await session.execute(
                        select(CertificateRenewalRow).order_by(
                            CertificateRenewalRow.started_at.desc()
                        )
                    )
                )
                .scalars()
                .all()
            )
        by_cert: dict[str, list[CertificateRenewalRow]] = {}
        for row in rows:
            by_cert.setdefault(row.certificate, []).append(row)
        out = []
        for cert in sorted(snapshot.certificates, key=lambda c: c.name):
            if cert.purpose != "server":
                continue
            history = by_cert.get(cert.name, [])
            streak = 0
            for row in history:
                if row.ok:
                    break
                streak += 1
            last = history[0] if history else None
            success = next((r for r in history if r.ok), None)
            out.append(
                RenewalStatus(
                    certificate_id=cert.id,
                    certificate=cert.name,
                    source=cert.source.value,
                    issuer=_issuer(cert),
                    not_after=cert.not_after,
                    renew_policy=cert.renew_policy.value,
                    renew_before_days=cert.renew_before_days,
                    renew_due_at=cert.not_after - timedelta(days=cert.renew_before_days),
                    last_attempt_at=last.started_at if last else None,
                    last_ok=last.ok if last else None,
                    last_error=last.error if last and not last.ok else None,
                    last_success_at=success.finished_at if success else None,
                    failures_in_a_row=streak,
                )
            )
        return out


def _issuer(cert: dm.Certificate) -> str:
    if cert.source is dm.CertSource.ACME:
        return cert.acme_provider or "ACME (default provider)"
    return {
        dm.CertSource.CLOUDFLARE_ORIGIN: "Cloudflare Origin CA",
        dm.CertSource.UPLOAD: "Uploaded",
        dm.CertSource.STEP_CA: "step-ca",
    }[cert.source]


async def record_attempt(
    db: Databases,
    *,
    tenant_id: UUID,
    certificate: str,
    trigger: str,
    started_at: datetime,
    ok: bool,
    error: str | None,
    change_set_id: UUID | None,
    not_after: datetime | None,
    acme_provider: str | None,
    challenge: str | None,
) -> None:
    """Store one issuance or renewal attempt."""
    from switchyard.ids import uuid7

    async with admin_session(db.admin) as session:
        session.add(
            CertificateRenewalRow(
                id=uuid7(),
                tenant_id=tenant_id,
                certificate=certificate,
                trigger=trigger,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                ok=ok,
                error=error[:4000] if error else None,
                change_set_id=change_set_id,
                not_after=not_after,
                acme_provider=acme_provider,
                challenge=challenge,
            )
        )
