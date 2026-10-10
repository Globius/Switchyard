"""Live status and drift detection.

``poll_all`` asks every enabled engine (HAProxy statistics and, where there is one, the agent) what
it is running and stores one ``engine_status`` row per engine. It also compares the reported
configuration with what the stored desired state renders to and records any difference as *drift*.
Drift is reported, never corrected (CLAUDE.md): a manual hotfix must be noticed, not erased.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select

from switchyard.adapters.agent import AgentEngine, AgentError, AgentStatus
from switchyard.adapters.haproxy import HaproxyEngine, HaproxyError
from switchyard.adapters.secrets import SecretsReader
from switchyard.db import audit_log
from switchyard.db.models import TenantRow
from switchyard.db.ops_models import EngineStatusRow, ServiceEngineRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot
from switchyard.pipeline.apply import bundle_id, file_hashes
from switchyard.pipeline.engines import EngineDirectory, EngineRecord
from switchyard.render.escape import RenderError
from switchyard.render.haproxy import render_haproxy
from switchyard.render.routing import EngineSlot, render_agent_bundle
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


@dataclass
class _Expected:
    fingerprint: str | None = None
    bundles: dict[str, dict[str, str]] = field(default_factory=dict)  # engine name -> hashes


def _agent_json(status: AgentStatus) -> dict[str, Any]:
    return {
        "version": status.agent_version,
        "bundle_id": status.bundle_id,
        "haproxy_healthy": status.haproxy_healthy,
        "routes_withdrawn": status.routes_withdrawn,
        "bgp": [{"peer": s.peer, "state": s.state, "remote_asn": s.remote_asn} for s in status.bgp],
        "vrrp": [{"name": v.name, "state": v.state} for v in status.vrrp],
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
        group = next((g for g in snapshot.se_groups if g.id == group_id), None)
        if group is None or group.routing_profile_id is None or out.fingerprint is None:
            return out
        secrets: dict[str, str] = {}
        for profile in snapshot.routing_profiles:
            if profile.id == group.routing_profile_id and isinstance(profile, dm.BgpProfile):
                for peer in profile.peers:
                    if peer.md5_ref and self._secrets is not None:
                        try:
                            secrets[peer.md5_ref] = await self._secrets.get(peer.md5_ref)
                        except LookupError:
                            return out
        for index, record in enumerate(records):
            try:
                files = render_agent_bundle(
                    snapshot,
                    group_id,
                    EngineSlot(record.name, index, record.mgmt_ip or "0.0.0.0"),  # noqa: S104
                    secrets,
                )
            except RenderError:
                continue
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
