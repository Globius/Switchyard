"""Health monitor test: run a monitor once from an engine against one pool member.

The probe runs on the engine, through the agent, because that is where the member is reachable and
what HAProxy itself sees. Nothing is stored. The agent refuses any target that is not a member of
its applied configuration, so this cannot be used to scan the network.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select

from switchyard.adapters.agent import AgentError, ProbeRequest
from switchyard.db.ops_models import ServiceEngineRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import tenant_session
from switchyard.domain.workflow import EngineState
from switchyard.pipeline.engines import EngineDirectory
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.engines import _record
from switchyard.services.errors import ConflictError, NotFoundError, PermissionDeniedError


@dataclass(frozen=True)
class MonitorTestResult:
    """Outcome of one monitor test."""

    ok: bool
    latency_ms: int
    detail: str
    engine: str
    target: str
    protocol: str


class MonitorTestService:
    """Runs health monitors once, from an engine."""

    def __init__(self, db: Databases, directory: EngineDirectory) -> None:
        """Create the service."""
        self._db = db
        self._directory = directory

    async def test(
        self,
        principal: Principal,
        tenant_id: UUID,
        monitor_id: UUID,
        *,
        pool_id: UUID,
        member_id: UUID,
        engine_id: UUID | None = None,
        port: int | None = None,
        http_path: str | None = None,
        expect_status: tuple[int, ...] | None = None,
        timeout_s: int | None = None,
    ) -> MonitorTestResult:
        """Probe ``member_id`` of ``pool_id`` the way ``monitor_id`` would.

        Overrides (``port``, ``http_path``, ``expect_status``, ``timeout_s``) replace the monitor's
        own values for this one run. Nothing is saved.

        Raises:
            NotFoundError: If the monitor, pool, member or engine does not exist.
            ConflictError: If no engine with an agent serves the pool, the monitor type does not
                suit the pool, or the agent refuses the target (it is not in the applied
                configuration yet, so apply first).
            AgentError: If the agent cannot be reached.
        """
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, "write", "application")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
            rows = list((await session.execute(select(ServiceEngineRow))).scalars())
        monitor = next((m for m in snapshot.health_monitors if m.id == monitor_id), None)
        if monitor is None:
            raise NotFoundError(f"health monitor {monitor_id} does not exist")
        pool = next((p for p in snapshot.pools if p.id == pool_id), None)
        member = next((m for m in pool.members if m.id == member_id), None) if pool else None
        if pool is None or member is None:
            raise NotFoundError("the pool or member does not exist")
        groups = {
            s.se_group_id
            for s in snapshot.virtual_services
            if pool_id in {r.pool_id for r in s.routes} | {s.default_pool_id}
        }
        candidates = [
            _record(r)
            for r in rows
            if r.se_group_id in groups and r.state == EngineState.ENABLED.value and r.agent_url
        ]
        if engine_id is not None:
            candidates = [c for c in candidates if c.id == engine_id]
            if not candidates:
                raise NotFoundError(f"engine {engine_id} does not serve this pool or has no agent")
        if not candidates:
            raise ConflictError("no enabled engine with an agent serves this pool")
        record = sorted(candidates, key=lambda c: c.name)[0]
        check_port = port or monitor.port or member.port
        if check_port is None:
            raise ConflictError("the member has no port and the monitor sets none: pass `port`")
        timeout = timeout_s or monitor.timeout_s
        http = monitor.http
        udp = monitor.udp
        request = ProbeRequest(
            protocol=monitor.type.value,
            address=str(member.address),
            port=check_port,
            timeout_ms=max(100, min(10_000, timeout * 1000)),
            http_method=http.method if http else "GET",
            http_path=http_path or (http.path if http else "/"),
            http_host=(http.host or "") if http else "",
            expect_status=expect_status
            if expect_status is not None
            else (http.expect_status if http else ()),
            udp_payload=bytes.fromhex(udp.payload_hex) if udp and udp.payload_hex else b"",
        )
        agent = await self._directory.connect_agent(record)
        if agent is None:
            raise ConflictError(f"engine {record.name} has no agent")
        try:
            result = await agent.probe(request)
        finally:
            await agent.close()
        if result.refused:
            raise ConflictError(
                "the engine's agent refused the target: it is not in the applied configuration "
                "yet (apply the change set that adds the member first)"
            )
        return MonitorTestResult(
            ok=result.ok,
            latency_ms=result.latency_ms,
            detail=result.detail,
            engine=record.name,
            target=f"{member.address}:{check_port}",
            protocol=request.protocol,
        )


__all__ = ["AgentError", "MonitorTestResult", "MonitorTestService"]
