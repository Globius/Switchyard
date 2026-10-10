"""Live status and drift detection.

``poll_all`` asks every enabled engine (HAProxy statistics and, where there is one, the agent) what
it is running and stores one ``engine_status`` row per engine. It also compares the reported
configuration with what the stored desired state renders to and records any difference as *drift*.
Drift is reported, never corrected (CLAUDE.md): a manual hotfix must be noticed, not erased.
"""

import asyncio
import dataclasses
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select

from switchyard.adapters.agent import AgentEngine, AgentError, AgentStatus, IpvsRealServer
from switchyard.adapters.haproxy import HaproxyEngine, HaproxyError
from switchyard.adapters.secrets import SecretsReader
from switchyard.db import audit_log
from switchyard.db.models import TenantRow
from switchyard.db.ops_models import EngineStatusRow, ServiceEngineRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain.models import Protocol
from switchyard.domain.snapshot import Snapshot
from switchyard.pipeline.apply import bundle_id, file_hashes
from switchyard.pipeline.bundles import BundleError, engine_files
from switchyard.pipeline.engines import EngineDirectory, EngineRecord
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import render_haproxy
from switchyard.render.routing import EngineSlot
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import PermissionDeniedError

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


def virtual_service_states(snapshot: Snapshot) -> list[VirtualServiceStatus]:
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
        out.append(VirtualServiceStatus(s.id, s.name, str(s.protocol), state))
    return sorted(out, key=lambda v: v.name)


@dataclass
class _Expected:
    fingerprint: str | None = None
    udp_members: dict[tuple[str, str, int], tuple[str, str]] = field(default_factory=dict)
    bundles: dict[str, dict[str, str]] = field(default_factory=dict)  # engine name -> hashes


def _agent_json(status: AgentStatus) -> dict[str, Any]:
    return {
        "version": status.agent_version,
        "bundle_id": status.bundle_id,
        "haproxy_healthy": status.haproxy_healthy,
        "routes_withdrawn": status.routes_withdrawn,
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
                members = [
                    {"backend": s.backend, "server": s.server, "status": s.status}
                    for s in await engine.server_statuses()
                ]
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
            return virtual_service_states(await load_snapshot(session))

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
