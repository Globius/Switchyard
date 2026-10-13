# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Publish a virtual service's VIP as DNS address records (ADR 0193).

A virtual service with a ``dns`` block lists host names. :class:`DnsRecordSync` makes each name
resolve to the service's VIP through the tenant's DNS credential (Cloudflare supports address
records; other provider types report ``error``). The ``dns_record`` table remembers what the
controller created: it changes or deletes only those records, so a record someone else made is
never overwritten (a name with a foreign record of another value is reported as ``error``). When a
service is disabled, deleted, loses a name or changes VIP, the records follow. The scheduled job
and ``POST /tenants/{t}/dns-records/sync`` run the same code.
"""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select

from switchyard.adapters.dns.base import DnsError, supports_addresses
from switchyard.db import audit_log
from switchyard.db.acme_models import DnsRecordRow
from switchyard.db.models import VirtualServiceRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.models import DnsPublication
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal, utcnow
from switchyard.services.dns_providers import DnsProviders, DnsResolver

log = logging.getLogger("switchyard.dns_records")

SYNC_DNS_RECORDS_JOB = "sync_dns_records"


@dataclass(frozen=True)
class DesiredRecord:
    """One address record a virtual service asks for."""

    service_id: UUID
    hostname: str
    rtype: str
    content: str
    ttl: int
    proxied: bool
    credential: str | None


def desired_records(rows: list[VirtualServiceRow]) -> dict[tuple[str, str], DesiredRecord]:
    """The records the enabled services with a ``dns`` block want, by ``(hostname, type)``.

    A name that two services list belongs to the one that sorts first by name.

    Example:
        >>> desired_records([])
        {}
    """
    out: dict[tuple[str, str], DesiredRecord] = {}
    for row in sorted(rows, key=lambda r: r.name):
        if not row.enabled or not row.dns:
            continue
        publication = DnsPublication.model_validate(row.dns)
        address = str(row.vip).split("/")[0]
        rtype = "AAAA" if ":" in address else "A"
        for name in publication.names:
            out.setdefault(
                (name.lower(), rtype),
                DesiredRecord(
                    row.id,
                    name.lower(),
                    rtype,
                    address,
                    publication.ttl,
                    publication.proxied,
                    publication.credential,
                ),
            )
    return out


class DnsRecordSync:
    """Keeps the address records of a tenant's virtual services in step with its provider."""

    def __init__(self, db: Databases, providers: DnsProviders) -> None:
        """Create the sync; ``providers`` builds the clients from the tenant's credentials."""
        self._db = db
        self._providers = providers

    async def list_records(self, principal: Principal, tenant_id: UUID) -> list[dict[str, Any]]:
        """The records the controller manages and their state (any role in the tenant)."""
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            names = {
                r.id: r.name for r in (await session.execute(select(VirtualServiceRow))).scalars()
            }
            rows = (
                (
                    await session.execute(
                        select(DnsRecordRow).order_by(DnsRecordRow.hostname, DnsRecordRow.rtype)
                    )
                )
                .scalars()
                .all()
            )
        return [
            {
                "hostname": r.hostname,
                "type": r.rtype,
                "content": r.content,
                "ttl": r.ttl,
                "proxied": r.proxied,
                "state": r.state,
                "virtual_service": names.get(r.virtual_service_id),
                "last_error": r.last_error,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]

    async def sync(
        self, principal: Principal, tenant_id: UUID, *, request_id: str | None = None
    ) -> dict[str, int]:
        """Reconcile the tenant's records now (tenant administrators)."""
        require(principal, "write", "infrastructure")
        result = await self.sync_tenant(tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="dns_record.sync",
                diff=result,
                request_id=request_id,
            )
        return result

    async def run(self) -> dict[str, int]:
        """The scheduled job: reconcile every tenant that publishes or has published records."""
        totals = {"tenants": 0, "written": 0, "deleted": 0, "failed": 0}
        async with admin_session(self._db.admin) as session:
            wanted = set(
                (
                    await session.execute(
                        select(VirtualServiceRow.tenant_id).where(VirtualServiceRow.dns.isnot(None))
                    )
                ).scalars()
            )
            wanted |= set((await session.execute(select(DnsRecordRow.tenant_id))).scalars())
        for tenant_id in sorted(wanted):
            try:
                result = await self.sync_tenant(tenant_id)
            except Exception:  # one tenant's trouble must not stop the others
                log.exception("dns record sync failed for %s", tenant_id)
                continue
            totals["tenants"] += 1
            for key in ("written", "deleted", "failed"):
                totals[key] += result[key]
        return totals

    async def sync_tenant(self, tenant_id: UUID) -> dict[str, int]:
        """Create, update and delete the tenant's records; returns the counts."""
        written = deleted = failed = 0
        resolvers: dict[str | None, DnsResolver] = {}

        async def provider(hostname: str, credential: str | None) -> Any:
            if credential not in resolvers:
                resolvers[credential] = self._providers.resolver(tenant_id, credential)
            found = await resolvers[credential].resolve(hostname)
            if not supports_addresses(found):
                raise DnsError(
                    "this DNS credential type cannot publish address records (only Cloudflare can)"
                )
            return found

        try:
            async with tenant_session(self._db.app, [tenant_id]) as session:
                services = list((await session.execute(select(VirtualServiceRow))).scalars())
                existing = {
                    (r.hostname, r.rtype): r
                    for r in (await session.execute(select(DnsRecordRow))).scalars()
                }
                want = desired_records(services)
                pinned = {
                    s.id: (s.dns or {}).get("credential") for s in services if s.dns is not None
                }
                for key, old in existing.items():
                    if key in want:
                        continue
                    try:
                        if old.handle:
                            client = await provider(
                                old.hostname, pinned.get(old.virtual_service_id)
                            )
                            await client.delete_address(old.hostname, old.handle)
                    except DnsError as exc:
                        old.state, old.last_error, old.updated_at = "error", str(exc), utcnow()
                        failed += 1
                        continue
                    await session.delete(old)
                    deleted += 1
                for key, record in sorted(want.items()):
                    found = existing.get(key)
                    unchanged = (
                        found is not None
                        and found.state == "published"
                        and (found.content, found.ttl, found.proxied, found.virtual_service_id)
                        == (record.content, record.ttl, record.proxied, record.service_id)
                    )
                    if unchanged:
                        continue
                    row = found
                    if row is None:
                        row = DnsRecordRow(
                            id=uuid7(),
                            tenant_id=tenant_id,
                            hostname=record.hostname,
                            rtype=record.rtype,
                            virtual_service_id=record.service_id,
                            content=record.content,
                            ttl=record.ttl,
                            proxied=record.proxied,
                        )
                        session.add(row)
                    row.virtual_service_id = record.service_id
                    row.content, row.ttl, row.proxied = record.content, record.ttl, record.proxied
                    try:
                        client = await provider(record.hostname, record.credential)
                        row.handle = await client.upsert_address(
                            record.hostname,
                            record.content,
                            ttl=record.ttl,
                            proxied=record.proxied,
                            handle=row.handle,
                        )
                    except DnsError as exc:
                        row.state, row.last_error = "error", str(exc)
                        failed += 1
                    else:
                        row.state, row.last_error = "published", None
                        written += 1
                    row.updated_at = utcnow()
        finally:
            for resolver in resolvers.values():
                await resolver.close()
        return {"written": written, "deleted": deleted, "failed": failed}
