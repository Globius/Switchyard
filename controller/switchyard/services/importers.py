# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Importing another vendor's configuration into a tenant (ADR 0150).

``AviImportService.run`` takes an Avi configuration export and the choices a migration needs (the SE
group, where new VIPs come from) and either reports what would be created (a dry run, the default)
or creates it as one **draft change set** for a person to review and apply, like any other change.
Certificates go through the certificate service, so their keys land in the secrets backend and
never in the change set, the database, a log or the Git mirror.
"""

import ipaddress
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import tenant_session
from switchyard.domain import ipam
from switchyard.domain.changes import Action, ChangeError, ChangeOp, ObjectType
from switchyard.ids import uuid7
from switchyard.importers.avi import AviOptions, AviPlan, Finding, convert, planned_ops_order
from switchyard.services.certificates import CertificateService
from switchyard.services.changesets import ChangeSetService, require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import FieldErrors, PermissionDeniedError

_KIND = {
    "health_monitor": ObjectType.HEALTH_MONITOR,
    "pool": ObjectType.POOL,
    "virtual_service": ObjectType.VIRTUAL_SERVICE,
}
MAX_EXPORT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ImportResult:
    """The outcome of an import run."""

    dry_run: bool
    change_set_id: UUID | None
    counts: dict[str, int]
    objects: list[dict[str, str]]
    vips: list[dict[str, Any]]
    findings: list[Finding]


class AviImportService:
    """Plan and create the objects of an Avi export."""

    def __init__(
        self, db: Databases, change_sets: ChangeSetService, certs: CertificateService | None
    ) -> None:
        """Create the service; ``certs`` is needed only to import certificates with keys."""
        self._db = db
        self._change_sets = change_sets
        self._certs = certs

    async def run(
        self,
        principal: Principal,
        tenant_id: UUID,
        export: dict[str, Any],
        *,
        se_group: str,
        vip_pool: str | None = None,
        vip_map: dict[str, str] | None = None,
        prefix: str = "",
        include_unused: bool = False,
        only: list[str] | None = None,
        dry_run: bool = True,
        request_id: str | None = None,
    ) -> ImportResult:
        """Convert ``export`` and, unless ``dry_run``, create a draft change set from it.

        Args:
            principal: Who imports (needs to edit drafts; certificates need ``manage certificate``).
            tenant_id: The tenant.
            export: The decoded Avi configuration export.
            se_group: Name of the SE group the services will use.
            vip_pool: VIP pool new VIPs are taken from, one per Avi VIP.
            vip_map: Fixed new addresses, keyed by Avi VIP name or by its old address.
            prefix: Put in front of every new name.
            include_unused: Also convert objects no service uses.
            only: Convert only these virtual services (by Avi name).
            dry_run: Report only.
            request_id: Request id for the audit log.

        Raises:
            FieldErrors: With the path of every unusable choice or rejected object.
        """
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, "edit", "draft")
        if len(json.dumps(export, default=str)) > MAX_EXPORT_BYTES:
            errors = FieldErrors()
            errors.add(
                "export", "too_large", "the export is larger than 64 MiB: send only what you need"
            )
            errors.raise_if_any()
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
        errors = FieldErrors()
        group = next((g for g in snapshot.se_groups if g.name == se_group), None)
        if group is None:
            errors.add("se_group", "not_found", f"no SE group named {se_group}")
        pool = (
            next((p for p in snapshot.vip_pools if p.name == vip_pool), None) if vip_pool else None
        )
        if vip_pool and pool is None:
            errors.add("vip_pool", "not_found", f"no VIP pool named {vip_pool}")
        fixed: dict[str, ipaddress.IPv4Address | ipaddress.IPv6Address] = {}
        for key, value in (vip_map or {}).items():
            try:
                fixed[key] = ipaddress.ip_address(value)
            except ValueError:
                errors.add(f"vip_map.{key}", "invalid", "must be an IP address")
        errors.raise_if_any()
        if group is None:  # unreachable: the error above was raised
            raise FieldErrors()
        network = next((n for n in snapshot.networks if pool and n.id == pool.network_id), None)
        used: list[Any] = [s.vip for s in snapshot.virtual_services] + list(fixed.values())
        cache: dict[str, str | None] = {}

        def vip_for(avi_name: str, old: str | None) -> str | None:
            if avi_name in cache:
                return cache[avi_name]
            chosen: Any = fixed.get(avi_name) or (fixed.get(old) if old else None)
            if chosen is None and pool is not None and network is not None:
                try:
                    chosen = ipam.allocate(pool, network, used)
                    used.append(chosen)
                except ValueError:
                    chosen = None
            cache[avi_name] = str(chosen) if chosen is not None else None
            return cache[avi_name]

        taken = {
            "virtual_service": frozenset(s.name for s in snapshot.virtual_services),
            "pool": frozenset(p.name for p in snapshot.pools),
            "health_monitor": frozenset(m.name for m in snapshot.health_monitors),
            "certificate": frozenset(c.name for c in snapshot.certificates),
        }
        plan = convert(
            export,
            AviOptions(
                se_group_id=group.id,
                vip_for=vip_for,
                prefix=prefix,
                include_unused=include_unused,
                only=frozenset(only or ()),
                taken=taken,
            ),
        )
        for need in plan.vips:
            if need.new_address is None:
                plan.note(
                    "manual",
                    f"vsvip:{need.avi_vsvip}",
                    "no new address (give vip_pool or vip_map): its services are left out",
                )
        if dry_run:
            return self._result(plan, None, dry_run=True)
        ops = await self._ops(principal, tenant_id, plan)
        change_set_id = await self._open(principal, tenant_id, ops, plan, request_id)
        return self._result(plan, change_set_id, dry_run=False)

    @staticmethod
    def _result(plan: AviPlan, change_set_id: UUID | None, *, dry_run: bool) -> ImportResult:
        objects = [
            {"kind": o.kind, "avi_name": o.avi_name, "name": o.name} for o in plan.objects
        ] + [
            {"kind": "certificate", "avi_name": c.avi_name, "name": c.name}
            for c in plan.certificates
        ]
        vips = [
            {
                "avi_vsvip": v.avi_vsvip,
                "address": v.address,
                "new_address": v.new_address,
                "services": v.services,
            }
            for v in plan.vips
        ]
        return ImportResult(dry_run, change_set_id, plan.counts(), objects, vips, plan.findings)

    async def _ops(self, principal: Principal, tenant_id: UUID, plan: AviPlan) -> list[ChangeOp]:
        vips = {v.avi_vsvip: v.new_address for v in plan.vips}
        keyed = [c for c in plan.certificates if c.key]
        if keyed and self._certs is None:
            errors = FieldErrors()
            errors.add(
                "export",
                "no_secrets_backend",
                "certificates with keys need the secrets backend (OpenBao); dry-run still works",
            )
            raise errors
        usable: set[str] = set()
        ops: list[ChangeOp] = []
        for cert in plan.certificates:
            if cert.key is None:
                plan.note(
                    "manual", f"certificate:{cert.avi_name}", "not imported: it has no private key"
                )
                continue
            body = await self._certs.import_pem(  # type: ignore[union-attr]
                principal, tenant_id, cert.name, cert.pem.rstrip() + "\n" + cert.key.strip() + "\n"
            )
            ops.append(
                ChangeOp(
                    object_type=ObjectType.CERTIFICATE,
                    object_id=cert.id,
                    action=Action.CREATE,
                    body=body,
                )
            )
            usable.add(str(cert.id))
        for obj in planned_ops_order(plan.objects):
            body = json.loads(json.dumps(obj.body))
            body["name"] = obj.name
            if obj.kind == "pool":
                for member in body.get("members", []):
                    member.setdefault("id", str(uuid7()))
            if obj.kind == "virtual_service":
                address = vips.get(str(body["vip"]).removeprefix("@vip:"))
                if address is None:
                    plan.note("skipped", f"virtual_service:{obj.avi_name}", "left out: no new VIP")
                    continue
                body["vip"] = address
                if "certificate_ids" in body:
                    body["certificate_ids"] = [c for c in body["certificate_ids"] if c in usable]
                    if not body["certificate_ids"]:
                        del body["certificate_ids"]
                if any(p.get("tls") for p in body["ports"]) and not body.get("certificate_ids"):
                    plan.note(
                        "manual",
                        f"virtual_service:{obj.avi_name}",
                        "left out: it needs TLS but no certificate could be imported",
                    )
                    continue
            ops.append(
                ChangeOp(
                    object_type=_KIND[obj.kind],
                    object_id=obj.id,
                    action=Action.CREATE,
                    body=body,
                )
            )
        return ops

    async def _open(
        self,
        principal: Principal,
        tenant_id: UUID,
        ops: list[ChangeOp],
        plan: AviPlan,
        request_id: str | None,
    ) -> UUID:
        opened = await self._change_sets.open(
            principal, tenant_id, f"Import from Avi: {len(ops)} object(s)", request_id=request_id
        )
        try:
            await self._change_sets.add_ops(
                principal, tenant_id, opened.record.id, ops, request_id=request_id
            )
        except ChangeError as exc:
            try:
                await self._change_sets.discard(principal, tenant_id, opened.record.id)
            finally:
                errors = FieldErrors()
                errors.add("export", "rejected", str(exc))
                raise errors from exc
        return opened.record.id
