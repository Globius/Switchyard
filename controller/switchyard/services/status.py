# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Live status and drift detection.

``poll_all`` asks every enabled engine (HAProxy statistics and, where there is one, the agent) what
it is running and stores one ``engine_status`` row per engine. It also compares the reported
configuration with what the stored desired state renders to and records any difference as *drift*.
Drift is reported, never corrected (CLAUDE.md): a manual hotfix must be noticed, not erased.
"""

import asyncio
import contextlib
import dataclasses
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select

from switchyard.adapters.agent import AgentEngine, AgentError, AgentStatus, IpvsRealServer
from switchyard.adapters.haproxy import HaproxyEngine, HaproxyError, ServerStatus
from switchyard.adapters.secrets import SecretsReader
from switchyard.db import audit_log
from switchyard.db.models import TenantRow
from switchyard.db.ops_models import EngineStatusRow, ServiceEngineRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.models import BgpProfile, Protocol, VirtualService
from switchyard.domain.snapshot import Snapshot
from switchyard.pipeline.apply import bundle_id, file_hashes
from switchyard.pipeline.bundles import BundleError, engine_files
from switchyard.pipeline.engines import EngineDirectory, EngineRecord
from switchyard.render.auth import auth_secret_refs, render_auth_files
from switchyard.render.escape import RenderError, ident
from switchyard.render.haproxy import fingerprint_of, render_haproxy
from switchyard.render.routing import EngineSlot, render_agent_bundle
from switchyard.render.tokens import jwt_key_files
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import NotFoundError, PermissionDeniedError

log = logging.getLogger("switchyard.status")


@dataclass(frozen=True)
class EngineStatus:
    """Stored status of one engine."""

    engine_id: UUID
    tenant_id: UUID
    se_group_id: UUID
    name: str
    reachable: bool
    error: str | None
    haproxy_fingerprint: str | None
    members: list[dict[str, str]]
    agent: dict[str, Any] | None
    drift: list[dict[str, str]]
    observed_at: datetime


def _member_row(s: ServerStatus) -> dict[str, str]:
    row = {"backend": s.backend, "server": s.server, "status": s.status}
    if s.check_status:
        row["check_status"] = s.check_status
    if s.last_check:
        row["last_check"] = s.last_check
    if s.last_change_s is not None:
        row["last_change_s"] = str(s.last_change_s)
    return row


_IPVS_STATUS = {"up": "UP", "down": "DOWN", "drain": "DRAIN"}
#: Worst state first: a pool member served on several UDP ports reports the worst of them.
_IPVS_ORDER = ("DOWN", "DRAIN", "UP")


def ipvs_members(
    reported: Iterable[IpvsRealServer], names: Mapping[tuple[str, str, int], tuple[str, str]]
) -> list[dict[str, str]]:
    """Turn IPVS real servers into HAProxy-shaped member rows (``backend``, ``server``, ``status``).

    Args:
        reported: What the agent reports.
        names: ``(service, member address, member port)`` to ``(pool backend, server name)``,
            built from the desired state; servers it does not know are skipped.

    Example:
        >>> row = IpvsRealServer("dns", "192.0.2.5", 53, "10.0.0.1", 53, "down")
        >>> ipvs_members([row], {("dns", "10.0.0.1", 53): ("pool_dns", "10-0-0-1-53")})
        [{'backend': 'pool_dns', 'server': '10-0-0-1-53', 'status': 'DOWN'}]
    """
    worst: dict[tuple[str, str], str] = {}
    for s in reported:
        key = names.get((s.service, s.address, s.member_port))
        if key is None:
            continue
        state = _IPVS_STATUS.get(s.state, "DOWN")
        if key not in worst or _IPVS_ORDER.index(state) < _IPVS_ORDER.index(worst[key]):
            worst[key] = state
    return [
        {"backend": backend, "server": server, "status": state}
        for (backend, server), state in sorted(worst.items())
    ]


def _udp_member_names(
    snapshot: Snapshot, group_id: UUID
) -> dict[tuple[str, str, int], tuple[str, str]]:
    pools = {p.id: p for p in snapshot.pools}
    out: dict[tuple[str, str, int], tuple[str, str]] = {}
    for svc in snapshot.virtual_services:
        if (
            svc.se_group_id != group_id
            or not svc.enabled
            or svc.protocol is not Protocol.UDP
            or svc.default_pool_id not in pools
        ):
            continue
        pool = pools[svc.default_pool_id]
        for m in pool.members:
            out[(svc.name, str(m.address), m.port or 0)] = (f"pool_{pool.name}", m.server_name)
    return out


@dataclass(frozen=True)
class VirtualServiceStatus:
    """Whether the data plane renders one virtual service.

    Attributes:
        id: Virtual service id.
        name: Virtual service name.
        protocol: ``http``, ``tcp`` or ``udp``.
        state: ``serving`` (some engine renders it: HAProxy, or IPVS for UDP), ``disabled``, or
            ``not_served`` (reserved for a service no engine can render; none today).
    """

    id: UUID
    name: str
    protocol: str
    state: str
    health: str = "unknown"
    members_up: int = 0
    members_total: int = 0
    members: tuple["MemberRow", ...] = ()


@dataclass(frozen=True)
class MemberRow:
    """One pool member as one engine reports it.

    Attributes:
        pool: Pool name.
        pool_id: Pool id.
        member_id: Pool member id.
        address: Member address.
        port: Member port, if the pool member has one.
        engine: Reporting engine name.
        engine_id: Reporting engine id.
        status: HAProxy status (``UP``, ``DOWN``, ...).
        check_status: Short code of the last health check, when the engine reports one.
        last_check: HAProxy description of the last check.
        last_change_s: Seconds since the status last changed.
        last_checked_at: When the controller last read this engine's check results.
    """

    pool: str
    pool_id: UUID
    member_id: UUID
    address: str
    port: int | None
    engine: str
    engine_id: UUID
    status: str
    check_status: str | None
    last_check: str | None
    last_change_s: int | None
    last_checked_at: datetime


def virtual_service_members(
    snapshot: Snapshot, service: VirtualService, statuses: Sequence["EngineStatus"]
) -> tuple[MemberRow, ...]:
    """List the reported member rows of the service pools, one per engine and member.

    Only reachable engines of the service SE group count, as for ``virtual_service_health``.
    Rows are sorted by pool, address, port and engine.
    """
    if not service.enabled:
        return ()
    pool_ids = {pid for r in service.routes for pid in r.pool_ids}
    if service.default_pool_id:
        pool_ids.add(service.default_pool_id)
    rows: list[MemberRow] = []
    for pool in (p for p in snapshot.pools if p.id in pool_ids):
        backend = "pool_" + ident(pool.name)
        by_server = {m.server_name: m for m in pool.members}
        for engine in statuses:
            if engine.se_group_id != service.se_group_id or not engine.reachable:
                continue
            for row in engine.members:
                member = by_server.get(row["server"]) if row["backend"] == backend else None
                if member is None:
                    continue
                changed = row.get("last_change_s")
                rows.append(
                    MemberRow(
                        pool=pool.name,
                        pool_id=pool.id,
                        member_id=member.id,
                        address=str(member.address),
                        port=member.port,
                        engine=engine.name,
                        engine_id=engine.engine_id,
                        status=row["status"],
                        check_status=row.get("check_status"),
                        last_check=row.get("last_check"),
                        last_change_s=int(changed) if changed is not None else None,
                        last_checked_at=engine.observed_at,
                    )
                )
    return tuple(sorted(rows, key=lambda r: (r.pool, r.address, r.port or 0, r.engine)))


def is_up(status: str) -> bool:
    """HAProxy reports `UP`, `UP 1/2`, `DOWN`, `DRAIN`, `MAINT`, `NOLB` and the like."""
    return status.upper().startswith("UP")


def virtual_service_health(
    snapshot: Snapshot, service: VirtualService, statuses: Sequence["EngineStatus"]
) -> tuple[str, int, int]:
    """Work out ``(health, members_up, members_total)`` of one virtual service.

    Rule (the API documents it too): look at the reported member rows of the service's pools
    (default pool and route pools) on the reachable engines of its SE group.

    - ``disabled``: the service is disabled.
    - ``unknown``: no engine of the group is reachable, or no engine has reported the pools yet.
    - ``healthy``: every reported row is ``UP`` and every reachable engine of the group reported.
    - ``down``: no row is ``UP``, or a pool without members.
    - ``degraded``: some rows are ``UP``, or some reachable engine of the group did not report.

    Draining and disabled members are not ``UP``, so draining the last member makes a service
    ``down`` on purpose.

    Example:
        >>> virtual_service_health(snapshot, service, [])  # doctest: +SKIP
        ('unknown', 0, 0)
    """
    if not service.enabled:
        return "disabled", 0, 0
    pool_ids = [pid for r in service.routes for pid in r.pool_ids]
    if service.default_pool_id:
        pool_ids.append(service.default_pool_id)
    pools = [p for p in snapshot.pools if p.id in set(pool_ids)]
    if not pools:
        return "healthy", 0, 0
    if any(not p.members for p in pools):
        return "down", 0, 0
    backends = {"pool_" + ident(p.name) for p in pools}
    engines = [s for s in statuses if s.se_group_id == service.se_group_id and s.reachable]
    if not engines:
        return "unknown", 0, 0
    rows = [[m for m in e.members if m["backend"] in backends] for e in engines]
    reported = [r for r in rows if r]
    flat = [m for r in reported for m in r]
    if not flat:
        return "unknown", 0, 0
    up = sum(1 for m in flat if is_up(m["status"]))
    if up == 0:
        health = "down"
    elif up == len(flat) and len(reported) == len(engines):
        health = "healthy"
    else:
        health = "degraded"
    return health, up, len(flat)


def virtual_service_states(
    snapshot: Snapshot, statuses: Sequence["EngineStatus"] = ()
) -> list[VirtualServiceStatus]:
    """Derive the state of every virtual service from the desired state.

    UDP services are ``serving`` since M6 (IPVS renders them). ``not_served`` stays in the contract
    for a service no engine can render, which is distinct from a failing service.

    Example:
        >>> virtual_service_states(snapshot)[0].state  # doctest: +SKIP
        'serving'
    """
    out = []
    for s in snapshot.virtual_services:
        state = "serving" if s.enabled else "disabled"
        health, up, total = virtual_service_health(snapshot, s, statuses)
        members = virtual_service_members(snapshot, s, statuses)
        out.append(
            VirtualServiceStatus(s.id, s.name, str(s.protocol), state, health, up, total, members)
        )
    return sorted(out, key=lambda v: v.name)


@dataclass
class _Expected:
    fingerprint: str | None = None
    config_text: str | None = None
    udp_members: dict[tuple[str, str, int], tuple[str, str]] = field(default_factory=dict)
    bundles: dict[str, dict[str, str]] = field(default_factory=dict)  # engine name -> hashes


def _same_text(a: str, b: str) -> bool:
    """Equal configuration text, ignoring line endings and trailing blanks."""
    return [x.rstrip() for x in a.strip().splitlines()] == [
        x.rstrip() for x in b.strip().splitlines()
    ]


def _agent_json(status: AgentStatus) -> dict[str, Any]:
    return {
        "version": status.agent_version,
        "bundle_id": status.bundle_id,
        "haproxy_healthy": status.haproxy_healthy,
        "routes_withdrawn": status.routes_withdrawn,
        "withdrawn_prefixes": list(status.withdrawn_prefixes),
        "waf_expected": status.waf_expected,
        "waf_running": status.waf_running,
        "bgp": [{"peer": s.peer, "state": s.state, "remote_asn": s.remote_asn} for s in status.bgp],
        "vrrp": [{"name": v.name, "state": v.state} for v in status.vrrp],
        "ipvs": [dataclasses.asdict(s) for s in status.ipvs],
        "file_hashes": dict(status.file_hashes),
    }


class StatusService:
    """Polls engines, stores their status and detects drift."""

    def __init__(
        self,
        db: Databases,
        directory: EngineDirectory,
        secrets: SecretsReader | None = None,
        *,
        concurrency: int = 8,
    ) -> None:
        """Create the service."""
        self._db = db
        self._directory = directory
        self._secrets = secrets
        self._limit = asyncio.Semaphore(concurrency)

    async def _expected(self, snapshot: Snapshot, records: list[EngineRecord]) -> _Expected:
        """What the stored desired state says one SE group's engines should be running."""
        out = _Expected()
        if not records:
            return out
        group_id = records[0].se_group_id
        try:
            rendered = render_haproxy(snapshot, group_id)
        except (KeyError, RenderError, ValueError):
            return out
        if "frontend vs_" in rendered.files["haproxy/haproxy.cfg"]:
            out.fingerprint = rendered.fingerprint
            out.config_text = rendered.files["haproxy/haproxy.cfg"]
        out.udp_members = _udp_member_names(snapshot, group_id)
        group = next((g for g in snapshot.se_groups if g.id == group_id), None)
        if group is None or out.fingerprint is None:
            return out
        for index, record in enumerate(records):
            slot = EngineSlot(record.name, index, record.mgmt_ip or "0.0.0.0")  # noqa: S104
            try:
                files = await engine_files(snapshot, group, slot, self._secrets)
            except BundleError:
                continue
            if files:
                out.bundles[record.name] = file_hashes(files) | {"__bundle_id__": bundle_id(files)}
        return out

    async def _poll_engine(
        self, record: EngineRecord, expected: _Expected
    ) -> tuple[
        bool,
        str | None,
        str | None,
        list[dict[str, str]],
        dict[str, Any] | None,
        list[dict[str, str]],
    ]:
        reachable, error = True, None
        fingerprint: str | None = None
        members: list[dict[str, str]] = []
        agent_json: dict[str, Any] | None = None
        drift: list[dict[str, str]] = []
        engine: HaproxyEngine | None = None
        agent: AgentEngine | None = None
        try:
            async with self._limit:
                engine = await self._directory.connect(record)
                fingerprint = await engine.fingerprint()
                members = [_member_row(s) for s in await engine.server_statuses()]
        except HaproxyError as exc:
            reachable, error = False, str(exc)
        finally:
            if engine is not None:
                await engine.close()
        if reachable and expected.fingerprint is not None and fingerprint != expected.fingerprint:
            drift.append(
                {
                    "kind": "haproxy_config",
                    "expected": expected.fingerprint,
                    "actual": fingerprint or "unmanaged",
                }
            )
        try:
            agent = await self._directory.connect_agent(record)
            if agent is not None:
                status = await agent.status()
                agent_json = _agent_json(status)
                members += ipvs_members(status.ipvs, expected.udp_members)
                wanted = expected.bundles.get(record.name)
                if wanted is not None:
                    if status.bundle_id != wanted["__bundle_id__"]:
                        drift.append(
                            {
                                "kind": "agent_bundle",
                                "expected": wanted["__bundle_id__"],
                                "actual": status.bundle_id or "none",
                            }
                        )
                    for path, digest in sorted(wanted.items()):
                        if path != "__bundle_id__" and status.file_hashes.get(path) != digest:
                            drift.append({"kind": "agent_file", "path": path})
                    for path in sorted(set(status.file_hashes) - set(wanted)):
                        drift.append({"kind": "agent_unexpected_file", "path": path})
        except AgentError as exc:
            reachable, error = False, f"{error + '; ' if error else ''}{exc}"
        finally:
            if agent is not None:
                await agent.close()
        return reachable, error, fingerprint, members, agent_json, drift

    async def poll_all(self) -> int:
        """Poll every enabled engine of every tenant. Returns how many engines were polled."""
        polled = 0
        async with admin_session(self._db.admin) as session:
            tenant_ids = list((await session.execute(select(TenantRow.id))).scalars())
        for tenant_id in tenant_ids:
            polled += await self.poll_tenant(tenant_id)
        return polled

    async def poll_tenant(self, tenant_id: UUID) -> int:
        """Poll one tenant's engines and store the results."""
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
            group_ids = [g.id for g in snapshot.se_groups]
            engine_ids = set((await session.execute(select(ServiceEngineRow.id))).scalars())
        previous = {s.engine_id: s for s in await self._stored(tenant_id)}
        by_group: dict[UUID, list[EngineRecord]] = {}
        for group_id in group_ids:
            records = [
                r
                for r in await self._directory.list_engines(tenant_id, group_id)
                if r.state.value == "enabled"
            ]
            if records:
                by_group[group_id] = records
        results: list[tuple[EngineRecord, Any]] = []
        for records in by_group.values():
            expected = await self._expected(snapshot, records)
            polled = await asyncio.gather(*(self._poll_engine(r, expected) for r in records))
            results.extend(zip(records, polled, strict=True))
        async with tenant_session(self._db.app, [tenant_id]) as session:
            await session.execute(
                delete(EngineStatusRow).where(
                    EngineStatusRow.engine_id.not_in(sorted(engine_ids) or [UUID(int=0)])
                )
            )
            for record, (reachable, error, fingerprint, members, agent_json, drift) in results:
                old = previous.get(record.id)
                row = await session.get(EngineStatusRow, record.id)
                if row is None:
                    row = EngineStatusRow(
                        engine_id=record.id, tenant_id=tenant_id, se_group_id=record.se_group_id
                    )
                    session.add(row)
                row.name = record.name
                row.reachable = reachable
                row.error = error
                row.haproxy_fingerprint = fingerprint
                row.members = members
                row.agent = agent_json
                row.drift = drift
                row.observed_at = func.now()
                if drift and (old is None or old.drift != drift):
                    log.warning("drift on %s: %s", record.name, drift)
                    await audit_log.record(
                        session,
                        tenant_id=tenant_id,
                        actor="system:drift_detector",
                        action="drift.detected",
                        object_ref=f"engine:{record.name}",
                        diff={"drift": drift},
                    )
                if not drift and old is not None and old.drift:
                    await audit_log.record(
                        session,
                        tenant_id=tenant_id,
                        actor="system:drift_detector",
                        action="drift.resolved",
                        object_ref=f"engine:{record.name}",
                    )
        return len(results)

    async def _stored(self, tenant_id: UUID) -> list[EngineStatus]:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = (await session.execute(select(EngineStatusRow))).scalars().all()
            return [_status(r) for r in rows]

    async def virtual_services(
        self, principal: Principal, tenant_id: UUID
    ) -> list[VirtualServiceStatus]:
        """Return the state of every virtual service of a tenant."""
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
        return virtual_service_states(snapshot, await self._stored(tenant_id))

    async def engines_of_service(
        self, principal: Principal, tenant_id: UUID, service_id: UUID
    ) -> list[EngineRecord]:
        """The enabled engines a virtual service is placed on (those of its SE group), by name.

        Raises:
            NotFoundError: If the service does not exist in the tenant.
        """
        self._read_check(principal, tenant_id)
        _, service = await self._service(tenant_id, service_id)
        return await self._enabled(tenant_id, service.se_group_id)

    async def rendered_for_engine(
        self, principal: Principal, tenant_id: UUID, service_id: UUID, engine_id: UUID
    ) -> tuple[EngineRecord, str, dict[str, str]]:
        """Everything rendered for one engine of a service's group, without any secret.

        Returns the engine, the render fingerprint and ``path -> content``: the HAProxy files (the
        same on every engine of the group) and the engine's own routing files (FRR or Keepalived,
        with BGP passwords replaced by ``REDACTED``). Certificates and keys are never included.

        Raises:
            NotFoundError: If the service or the engine (of the service's group) is not found.
        """
        self._read_check(principal, tenant_id)
        snapshot, service = await self._service(tenant_id, service_id)
        engines = await self._enabled(tenant_id, service.se_group_id)
        index = next((i for i, e in enumerate(engines) if e.id == engine_id), None)
        if index is None:
            raise NotFoundError(f"engine {engine_id} does not serve this service")
        record = engines[index]
        group = next(g for g in snapshot.se_groups if g.id == service.se_group_id)
        rendered = render_haproxy(snapshot, group.id)
        files = dict(rendered.files)
        if group.routing_profile_id is not None:
            slot = EngineSlot(record.name, index, record.mgmt_ip or "0.0.0.0")  # noqa: S104
            refs = {
                peer.md5_ref: "REDACTED"
                for profile in snapshot.routing_profiles
                if profile.id == group.routing_profile_id and isinstance(profile, BgpProfile)
                for peer in profile.peers
                if peer.md5_ref
            }
            # a group that cannot render its routing yet shows the HAProxy files only
            with contextlib.suppress(RenderError):
                files |= render_agent_bundle(snapshot, group.id, slot, refs)
        # access sets: the proxy configs, with their secrets shown as REDACTED
        placeholders = dict.fromkeys(
            auth_secret_refs(snapshot.for_se_group(group.id), group.id), "REDACTEDREDACTED"
        )
        with contextlib.suppress(RenderError):
            files |= render_auth_files(snapshot, group.id, placeholders)
        with contextlib.suppress(RenderError):
            files |= jwt_key_files(snapshot, group.id)
        return record, rendered.fingerprint, files

    async def running_config(
        self, principal: Principal, tenant_id: UUID, engine_id: UUID
    ) -> dict[str, Any]:
        """Ask one engine what it is running and compare it with the desired state.

        HAProxy's configuration comes from its Data Plane API. The agent has no call that reads
        files back (by design), so for the routing files it reports their hashes, which are
        compared with the hashes of what the controller would send now.

        Raises:
            NotFoundError: If the engine is not in the tenant.
        """
        self._read_check(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
            row = (
                await session.execute(
                    select(ServiceEngineRow).where(ServiceEngineRow.id == engine_id)
                )
            ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"engine {engine_id} not found")
        engines = await self._enabled(tenant_id, row.se_group_id)
        record = next((e for e in engines if e.id == engine_id), None)
        if record is None:  # disabled engines are not part of rollouts; look it up anyway
            record = next(
                e
                for e in await self._directory.list_engines(tenant_id, row.se_group_id)
                if e.id == engine_id
            )
        expected = await self._expected(snapshot, engines or [record])
        out: dict[str, Any] = {
            "engine": record.name,
            "engine_id": record.id,
            "se_group_id": record.se_group_id,
            "reachable": True,
            "error": None,
            "haproxy": None,
            "agent": None,
        }
        engine = None
        agent = None
        try:
            async with self._limit:
                engine = await self._directory.connect(record)
                content = await engine.running_config()
            if content is not None:
                stamp = fingerprint_of(content)
                same_stamp = expected.fingerprint is not None and stamp == expected.fingerprint
                out["haproxy"] = {
                    "content": content,
                    "fingerprint": stamp,
                    "desired_fingerprint": expected.fingerprint,
                    "fingerprint_matches": same_stamp,
                    "matches_desired": same_stamp
                    and _same_text(content, expected.config_text or ""),
                }
        except HaproxyError as exc:
            log.warning("running config of %s: %s", record.name, exc)
            out["reachable"], out["error"] = False, str(exc)
        finally:
            if engine is not None:
                await engine.close()
        try:
            agent = await self._directory.connect_agent(record)
            if agent is not None:
                status = await agent.status()
                wanted = expected.bundles.get(record.name, {})
                paths = sorted(
                    set(status.file_hashes) | {p for p in wanted if p != "__bundle_id__"}
                )
                out["agent"] = {
                    "bundle_id": status.bundle_id or None,
                    "desired_bundle_id": wanted.get("__bundle_id__"),
                    "files": [
                        {
                            "path": p,
                            "sha256": status.file_hashes.get(p),
                            "desired_sha256": wanted.get(p),
                            "matches_desired": status.file_hashes.get(p) == wanted.get(p),
                        }
                        for p in paths
                    ],
                }
        except AgentError as exc:
            log.warning("agent status of %s: %s", record.name, exc)
            out["reachable"], out["error"] = (
                False,
                f"{out['error'] + '; ' if out['error'] else ''}{exc}",
            )
        finally:
            if agent is not None:
                await agent.close()
        out["fetched_at"] = datetime.now(UTC)
        return out

    def _read_check(self, principal: Principal, tenant_id: UUID) -> None:
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")

    async def _service(self, tenant_id: UUID, service_id: UUID) -> tuple[Snapshot, VirtualService]:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
        service = next((s for s in snapshot.virtual_services if s.id == service_id), None)
        if service is None:
            raise NotFoundError(f"virtual service {service_id} not found")
        return snapshot, service

    async def _enabled(self, tenant_id: UUID, group_id: UUID) -> list[EngineRecord]:
        return [
            r
            for r in await self._directory.list_engines(tenant_id, group_id)
            if r.state.value == "enabled"
        ]

    async def list_status(self, principal: Principal, tenant_id: UUID) -> list[EngineStatus]:
        """Return the stored status of a tenant's engines."""
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        return sorted(await self._stored(tenant_id), key=lambda s: (str(s.se_group_id), s.name))


def _status(row: EngineStatusRow) -> EngineStatus:
    return EngineStatus(
        engine_id=row.engine_id,
        tenant_id=row.tenant_id,
        se_group_id=row.se_group_id,
        name=row.name,
        reachable=row.reachable,
        error=row.error,
        haproxy_fingerprint=row.haproxy_fingerprint,
        members=list(row.members),
        agent=row.agent,
        drift=list(row.drift),
        observed_at=row.observed_at,
    )
