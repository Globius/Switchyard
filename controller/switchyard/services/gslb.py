"""GSLB: zones and wide IPs on PowerDNS (ADR 0160 to 0163).

A system administrator creates a zone for a tenant (:meth:`GslbService.create_zone`); the tenant
manages wide IPs inside it. Writes go to the database and then to PowerDNS at once; a failed push is
recorded on the wide IP (``last_error``) and fixed by ``sync``. :class:`GslbSync` is the scheduled
job: when a member names a virtual service and its VIP moved, the records change with it. It never
rewrites records that merely differ in PowerDNS: that is drift, which is reported by
:meth:`GslbService.status` and fixed only by an explicit ``sync`` (drift is reported, not fixed).
"""

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select

from switchyard.adapters.powerdns import PowerDns, PowerDnsError, fqdn
from switchyard.db import audit_log
from switchyard.db.gslb_models import GslbWideIpRow, GslbZoneRow
from switchyard.db.models import TenantRow, VirtualServiceRow
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.gslb import (
    GslbInvalid,
    Rrset,
    next_order,
    render_rrsets,
    validate_label,
    validate_wide_ip,
    validate_zone_name,
)
from switchyard.ids import uuid7
from switchyard.services.changesets import require
from switchyard.services.context import Clock, Databases, Principal, utcnow
from switchyard.services.errors import AlreadyExistsError, FieldErrors, NotFoundError

log = logging.getLogger("switchyard.gslb")

SYNC_GSLB_JOB = "sync_gslb"
PROBE_GSLB_JOB = "probe_gslb"


def _raise(exc: GslbInvalid) -> FieldErrors:
    errors = FieldErrors()
    for e in exc.errors:
        errors.add(e["path"], e["code"], e["message"])
    return errors


def _fqdn(label: str, zone: str) -> str:
    return fqdn(zone) if label == "@" else f"{label}.{fqdn(zone)}"


def _digest(rrsets: list[Rrset]) -> str:
    body = [[r.name, r.type, r.ttl, r.records] for r in rrsets]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _zone_json(row: GslbZoneRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "nameservers": row.nameservers,
        "dnssec": row.dnssec,
        "version": row.version,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


def _wide_json(row: GslbWideIpRow, zone: str) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "zone": zone,
        "fqdn": _fqdn(row.name, zone).rstrip("."),
        "algorithm": row.spec["algorithm"],
        "ttl": row.spec["ttl"],
        "health": row.spec["health"],
        "members": row.spec["members"],
        "enabled": row.enabled,
        "version": row.version,
        "applied_at": row.applied_at,
        "last_error": row.last_error,
        "created_by": row.created_by,
        "created_at": row.created_at,
    }


class GslbService:
    """Zones and wide IPs, and keeping PowerDNS in step with them."""

    def __init__(self, db: Databases, dns: PowerDns, clock: Clock = utcnow) -> None:
        """Create the service over the database and the PowerDNS adapter."""
        self._db = db
        self._dns = dns
        self._clock = clock

    # -- zones -------------------------------------------------------

    async def list_zones(self, principal: Principal, tenant_id: UUID) -> list[dict[str, Any]]:
        """The tenant's zones (any role in the tenant)."""
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = (await session.execute(select(GslbZoneRow).order_by(GslbZoneRow.name))).scalars()
            return [_zone_json(r) for r in rows]

    async def create_zone(
        self,
        principal: Principal,
        tenant_id: UUID,
        name: str,
        nameservers: list[str],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a zone for a tenant (system administrators only).

        The zone must not overlap a zone of another tenant (a parent or a child), so nobody can
        shadow somebody else's names. Delegate the zone to the nameservers at its parent yourself.
        """
        require(principal, "manage", "tenant")
        errors = FieldErrors()
        try:
            clean = validate_zone_name(name)
        except GslbInvalid as exc:
            raise _raise(exc) from exc
        servers: list[str] = []
        for i, ns in enumerate(nameservers):
            try:
                servers.append(validate_zone_name(ns))
            except GslbInvalid:
                errors.add(f"nameservers[{i}]", "invalid", "not a host name")
        if not servers:
            errors.add("nameservers", "invalid", "name at least one nameserver")
        errors.raise_if_any()
        async with admin_session(self._db.admin) as session:
            for (other,) in (await session.execute(select(GslbZoneRow.name))).all():
                if other == clean or other.endswith("." + clean) or clean.endswith("." + other):
                    raise AlreadyExistsError(f"zone {clean!r} overlaps the existing zone {other!r}")
            if (await session.get(TenantRow, tenant_id)) is None:
                raise NotFoundError("tenant not found")
            row = GslbZoneRow(
                id=uuid7(),
                tenant_id=tenant_id,
                name=clean,
                nameservers=servers,
                created_by=principal.actor,
            )
            session.add(row)
            await session.flush()
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="gslb_zone.create",
                object_ref=f"gslb_zone:{clean}",
                request_id=request_id,
            )
            await self._dns.ensure_zone(clean, servers)
            await session.refresh(row)
            return _zone_json(row)

    async def delete_zone(
        self, principal: Principal, tenant_id: UUID, name: str, request_id: str | None = None
    ) -> None:
        """Delete a zone and every wide IP in it (system administrators only)."""
        require(principal, "manage", "tenant")
        async with admin_session(self._db.admin) as session:
            row = (
                await session.execute(
                    select(GslbZoneRow).where(
                        GslbZoneRow.tenant_id == tenant_id, GslbZoneRow.name == name
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"zone {name!r} not found")
            await session.execute(delete(GslbZoneRow).where(GslbZoneRow.id == row.id))
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="gslb_zone.delete",
                object_ref=f"gslb_zone:{name}",
                request_id=request_id,
            )
            await self._dns.delete_zone(name)

    async def enable_dnssec(
        self, principal: Principal, tenant_id: UUID, name: str, request_id: str | None = None
    ) -> dict[str, Any]:
        """Sign the zone; return the DS records to publish at the parent (tenant administrators)."""
        require(principal, "write", "infrastructure")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await self._zone(session, name)
            ds = await self._dns.enable_dnssec(name)
            row.dnssec = True
            row.version += 1
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="gslb_zone.dnssec",
                object_ref=f"gslb_zone:{name}",
                request_id=request_id,
            )
        return {"zone": name, "dnssec": True, "ds": ds}

    async def ds_records(self, principal: Principal, tenant_id: UUID, name: str) -> dict[str, Any]:
        """The zone's DS records (any role in the tenant)."""
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await self._zone(session, name)
            dnssec = row.dnssec
        return {"zone": name, "dnssec": dnssec, "ds": await self._dns.ds_records(name)}

    async def _zone(self, session: Any, name: str) -> GslbZoneRow:
        row: GslbZoneRow | None = (
            await session.execute(select(GslbZoneRow).where(GslbZoneRow.name == name))
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"zone {name!r} not found")
        return row

    # -- wide IPs ---------------------------------------------------------------------

    async def _vips(self, session: Any) -> dict[str, str]:
        rows = (
            await session.execute(
                select(VirtualServiceRow.name, VirtualServiceRow.vip).where(
                    VirtualServiceRow.vip.is_not(None)
                )
            )
        ).all()
        return {name: str(vip).split("/")[0] for name, vip in rows}

    def _records(self, row: GslbWideIpRow, zone: str, vips: dict[str, str]) -> list[Rrset]:
        addresses = {
            i: vips[m["virtual_service"]]
            for i, m in enumerate(row.spec["members"])
            if m.get("virtual_service") in vips
        }
        order = (row.measurements or {}).get("order")
        return render_rrsets(_fqdn(row.name, zone), row.spec, addresses=addresses, order=order)

    async def list_wide_ips(
        self, principal: Principal, tenant_id: UUID, zone: str | None = None
    ) -> list[dict[str, Any]]:
        """The tenant's wide IPs, optionally of one zone (any role in the tenant)."""
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            zones = {z.id: z.name for z in (await session.execute(select(GslbZoneRow))).scalars()}
            query = select(GslbWideIpRow).order_by(GslbWideIpRow.zone_id, GslbWideIpRow.name)
            return [
                _wide_json(r, zones[r.zone_id])
                for r in (await session.execute(query)).scalars()
                if zone in (None, zones[r.zone_id])
            ]

    async def create_wide_ip(
        self,
        principal: Principal,
        tenant_id: UUID,
        zone: str,
        name: str,
        spec: dict[str, Any],
        *,
        enabled: bool = True,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a wide IP and publish it (tenant administrators)."""
        require(principal, "write", "infrastructure")
        try:
            label = validate_label(name)
        except GslbInvalid as exc:
            raise _raise(exc) from exc
        async with tenant_session(self._db.app, [tenant_id]) as session:
            zrow = await self._zone(session, zone)
            vips = await self._vips(session)
            try:
                clean = validate_wide_ip(spec, resolve=vips.get)
            except GslbInvalid as exc:
                raise _raise(exc) from exc
            if (
                await session.execute(
                    select(GslbWideIpRow.id).where(
                        GslbWideIpRow.zone_id == zrow.id, GslbWideIpRow.name == label
                    )
                )
            ).first():
                raise AlreadyExistsError(f"{_fqdn(label, zone).rstrip('.')} already exists")
            row = GslbWideIpRow(
                id=uuid7(),
                tenant_id=tenant_id,
                zone_id=zrow.id,
                name=label,
                spec=clean,
                enabled=enabled,
                created_by=principal.actor,
            )
            session.add(row)
            await session.flush()
            await self._apply(row, zone, vips)
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="gslb_wide_ip.create",
                object_ref=f"gslb_wide_ip:{_fqdn(label, zone).rstrip('.')}",
                diff={"algorithm": clean["algorithm"]},
                request_id=request_id,
            )
            await session.flush()
            await session.refresh(row)
            return _wide_json(row, zone)

    async def update_wide_ip(
        self,
        principal: Principal,
        tenant_id: UUID,
        zone: str,
        name: str,
        *,
        spec: dict[str, Any] | None = None,
        enabled: bool | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Change a wide IP or switch it off and on; republishes (tenant administrators)."""
        require(principal, "write", "infrastructure")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await self._wide(session, zone, name)
            vips = await self._vips(session)
            if spec is not None:
                try:
                    row.spec = validate_wide_ip({**row.spec, **spec}, resolve=vips.get)
                except GslbInvalid as exc:
                    raise _raise(exc) from exc
            if enabled is not None:
                row.enabled = enabled
            row.version += 1
            await self._apply(row, zone, vips)
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="gslb_wide_ip.update",
                object_ref=f"gslb_wide_ip:{_fqdn(name, zone).rstrip('.')}",
                request_id=request_id,
            )
            await session.flush()
            return _wide_json(row, zone)

    async def delete_wide_ip(
        self,
        principal: Principal,
        tenant_id: UUID,
        zone: str,
        name: str,
        request_id: str | None = None,
    ) -> None:
        """Remove a wide IP and its records (tenant administrators)."""
        require(principal, "write", "infrastructure")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await self._wide(session, zone, name)
            await session.execute(delete(GslbWideIpRow).where(GslbWideIpRow.id == row.id))
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="gslb_wide_ip.delete",
                object_ref=f"gslb_wide_ip:{_fqdn(name, zone).rstrip('.')}",
                request_id=request_id,
            )
            await self._dns.delete_rrsets(zone, [(_fqdn(name, zone), "LUA")])

    async def _wide(self, session: Any, zone: str, name: str) -> GslbWideIpRow:
        zrow = await self._zone(session, zone)
        row: GslbWideIpRow | None = (
            await session.execute(
                select(GslbWideIpRow).where(
                    GslbWideIpRow.zone_id == zrow.id, GslbWideIpRow.name == name
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"{_fqdn(name, zone).rstrip('.')} not found")
        return row

    async def _apply(self, row: GslbWideIpRow, zone: str, vips: dict[str, str]) -> None:
        """Write the wide IP's records (or remove them when it is off); record the outcome."""
        now = self._clock()
        try:
            if row.enabled:
                rrsets = self._records(row, zone, vips)
                await self._dns.put_rrsets(zone, rrsets)
                row.applied_hash = _digest(rrsets)
            else:
                await self._dns.delete_rrsets(zone, [(_fqdn(row.name, zone), "LUA")])
                row.applied_hash = None
            row.applied_at, row.last_error = now, None
        except (PowerDnsError, GslbInvalid) as exc:
            row.last_error = (
                str(exc) if isinstance(exc, PowerDnsError) else "a member has no address"
            )

    # -- status and sync ---------------------------------------------

    async def status(self, principal: Principal, tenant_id: UUID) -> list[dict[str, Any]]:
        """Each wide IP against PowerDNS: ``in_sync``, ``drift``, ``pending``, ``error`` or ``off``.

        ``drift`` means the records in PowerDNS differ from what the controller would write now
        (someone edited them, or a VIP moved and the sync has not run yet). Nothing is changed.
        """
        require(principal, "read", "tenant_data")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            zones = {z.id: z.name for z in (await session.execute(select(GslbZoneRow))).scalars()}
            vips = await self._vips(session)
            rows = list((await session.execute(select(GslbWideIpRow))).scalars())
            actual: dict[str, dict[str, Rrset]] = {}
            reachable = True
            for zname in set(zones.values()):
                try:
                    actual[zname] = {r.name: r for r in await self._dns.get_rrsets(zname)}
                except PowerDnsError:
                    reachable = False
            out = []
            for row in sorted(rows, key=lambda r: (zones[r.zone_id], r.name)):
                zone = zones[row.zone_id]
                name = _fqdn(row.name, zone)
                live = actual.get(zone, {}).get(name)
                if row.last_error:
                    state = "error"
                elif not reachable:
                    state = "unknown"
                elif not row.enabled:
                    state = "off" if live is None else "drift"
                elif row.applied_hash is None:
                    state = "pending"
                else:
                    try:
                        want = self._records(row, zone, vips)[0]
                        state = (
                            "in_sync"
                            if live is not None
                            and live.records == want.records
                            and live.ttl == want.ttl
                            else "drift"
                        )
                    except GslbInvalid:
                        state = "error"
                out.append(
                    {
                        "fqdn": name.rstrip("."),
                        "state": state,
                        "last_error": row.last_error,
                        "applied_at": row.applied_at,
                    }
                )
            return out

    async def sync(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        force: bool = True,
        request_id: str | None = None,
    ) -> dict[str, int]:
        """Write every enabled wide IP's records again (tenant administrators)."""
        require(principal, "write", "infrastructure")
        result = await self._sync_tenant(tenant_id, force=force)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="gslb_wide_ip.sync",
                diff=result,
                request_id=request_id,
            )
        return result

    async def _sync_tenant(self, tenant_id: UUID, *, force: bool) -> dict[str, int]:
        written = failed = 0
        async with tenant_session(self._db.app, [tenant_id]) as session:
            zones = {z.id: z.name for z in (await session.execute(select(GslbZoneRow))).scalars()}
            vips = await self._vips(session)
            for row in (await session.execute(select(GslbWideIpRow))).scalars():
                zone = zones[row.zone_id]
                if not row.enabled:
                    continue
                try:
                    want = _digest(self._records(row, zone, vips))
                except GslbInvalid:
                    row.last_error = "a member has no address"
                    failed += 1
                    continue
                if not force and want == row.applied_hash and row.last_error is None:
                    continue
                await self._apply(row, zone, vips)
                if row.last_error:
                    failed += 1
                else:
                    written += 1
        return {"written": written, "failed": failed}


class GslbSync:
    """The scheduled job: follow VIP moves and retry failed pushes; leave drift alone."""

    def __init__(self, service: GslbService, db: Databases) -> None:
        """Create the job."""
        self._service = service
        self._db = db

    async def run(self) -> dict[str, int]:
        """Sync every tenant that has zones; returns the totals."""
        totals = {"tenants": 0, "written": 0, "failed": 0}
        async with admin_session(self._db.admin) as session:
            tenants = list(
                (await session.execute(select(GslbZoneRow.tenant_id).distinct())).scalars()
            )
        for tenant_id in tenants:
            try:
                r = await self._service._sync_tenant(tenant_id, force=False)
            except Exception:  # one tenant's trouble must not stop the others
                log.exception("gslb sync failed for %s", tenant_id)
                continue
            totals["tenants"] += 1
            totals["written"] += r["written"]
            totals["failed"] += r["failed"]
        return totals


Connect = Callable[[str, int, float], Awaitable[float | None]]


async def tcp_connect_ms(address: str, port: int, timeout: float) -> float | None:
    """Milliseconds to open a TCP connection to ``address:port``, or ``None`` if it fails."""
    started = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(address, port), timeout)
    except (OSError, TimeoutError):
        return None
    elapsed = (time.perf_counter() - started) * 1000
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return elapsed


class GslbProber:
    """The scheduled job behind the ``latency`` algorithm (ADR 0165).

    Every run it times a TCP connect to each member's health port, from this controller, smooths the
    result (``SMOOTHING`` weight on the new sample) and stores it. The members' order follows with
    hysteresis (:func:`switchyard.domain.gslb.next_order`); records are rewritten only when the
    order changes them.
    """

    SAMPLES = 3
    SMOOTHING = 0.5

    def __init__(
        self, service: GslbService, db: Databases, connect: Connect = tcp_connect_ms
    ) -> None:
        """Create the prober; ``connect`` is replaceable in tests."""
        self._service = service
        self._db = db
        self._connect = connect

    async def run(self) -> dict[str, int]:
        """Probe every enabled latency wide IP; returns how many were probed and rewritten."""
        totals = {"probed": 0, "rewritten": 0}
        async with admin_session(self._db.admin) as session:
            tenants = list(
                (await session.execute(select(GslbZoneRow.tenant_id).distinct())).scalars()
            )
        for tenant_id in tenants:
            try:
                r = await self._tenant(tenant_id)
            except Exception:  # one tenant's trouble must not stop the others
                log.exception("gslb probe failed for %s", tenant_id)
                continue
            totals["probed"] += r[0]
            totals["rewritten"] += r[1]
        return totals

    async def _tenant(self, tenant_id: UUID) -> tuple[int, int]:
        probed = rewritten = 0
        service = self._service
        async with tenant_session(self._db.app, [tenant_id]) as session:
            zones = {z.id: z.name for z in (await session.execute(select(GslbZoneRow))).scalars()}
            vips = await service._vips(session)
            rows = (
                await session.execute(select(GslbWideIpRow).where(GslbWideIpRow.enabled.is_(True)))
            ).scalars()
            for row in rows:
                if row.spec["algorithm"] != "latency":
                    continue
                before = _digest(service._records(row, zones[row.zone_id], vips))
                port = (row.spec.get("health") or {}).get("port", 443)
                timeout = (row.spec.get("health") or {}).get("timeout_ms", 1000) / 1000
                previous = (row.measurements or {}).get("ms", {})
                ms: dict[int, float | None] = {}
                for i, m in enumerate(row.spec["members"]):
                    address = m.get("address") or vips.get(m.get("virtual_service", ""))
                    if address is None:
                        ms[i] = None
                        continue
                    samples = [
                        await self._connect(address, port, timeout) for _ in range(self.SAMPLES)
                    ]
                    ok = sorted(s for s in samples if s is not None)
                    if not ok:
                        ms[i] = None
                        continue
                    median = ok[len(ok) // 2]
                    old = previous.get(str(i))
                    ms[i] = median if old is None else old + self.SMOOTHING * (median - old)
                order = next_order((row.measurements or {}).get("order", []), ms)
                row.measurements = {
                    "ms": {str(i): v for i, v in ms.items() if v is not None},
                    "order": order,
                }
                probed += 1
                if _digest(service._records(row, zones[row.zone_id], vips)) != before:
                    await service._apply(row, zones[row.zone_id], vips)
                    rewritten += 1
        return probed, rewritten


__all__ = [
    "PROBE_GSLB_JOB",
    "SYNC_GSLB_JOB",
    "GslbProber",
    "GslbService",
    "GslbSync",
    "datetime",
    "tcp_connect_ms",
]
