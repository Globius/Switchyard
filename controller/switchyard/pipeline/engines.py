"""Which service engines serve an SE group, and how to reach them."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID

from sqlalchemy import select

from switchyard.adapters.agent import AgentEngine, BundleSigner, GrpcAgent
from switchyard.adapters.dataplane import DataPlaneEngine
from switchyard.adapters.haproxy import HaproxyEngine
from switchyard.adapters.secrets import SecretsReader
from switchyard.db.ops_models import ServiceEngineRow
from switchyard.db.tenancy import tenant_session
from switchyard.domain.workflow import EngineState
from switchyard.services.context import Databases


@dataclass(frozen=True)
class EngineRecord:
    """A registered service engine.

    Attributes:
        id: Row id.
        tenant_id: Owning tenant.
        se_group_id: The group it serves.
        name: Unique name within the group; engines roll out in name order, the first is the canary.
        dataplane_url: ``https://host:port`` of its Data Plane API.
        credentials_ref: Secret reference holding ``user:password`` for the API, if it needs any.
        state: ``enabled`` engines take part in rollouts.
        agent_url: ``host:port`` of the engine's ``sy-agent`` (gRPC, mTLS); ``None`` for an engine
            that only runs HAProxy (routing is then managed outside Switchyard).
        mgmt_ip: Management address; used as the BGP router id.
    """

    id: UUID
    tenant_id: UUID
    se_group_id: UUID
    name: str
    dataplane_url: str
    credentials_ref: str | None
    state: EngineState
    agent_url: str | None = None
    mgmt_ip: str | None = None


class EngineDirectory(Protocol):
    """Finds and connects to the engines of an SE group."""

    async def list_engines(self, tenant_id: UUID, se_group_id: UUID) -> list[EngineRecord]:
        """Return the group's engines sorted by name (the first enabled one is the canary)."""
        ...

    async def connect(self, record: EngineRecord) -> HaproxyEngine:
        """Open a client for ``record``. The caller closes it."""
        ...

    async def connect_agent(self, record: EngineRecord) -> AgentEngine | None:
        """Open an agent client, or return ``None`` if the engine has no agent."""
        ...


@dataclass(frozen=True)
class DataPlaneTls:
    """How the controller authenticates to Data Plane APIs.

    Attributes:
        ca_file: CA that signed the engines' API certificates.
        client_cert: ``(certificate, key)`` of the controller's client certificate.
    """

    ca_file: Path | None = None
    client_cert: tuple[Path, Path] | None = None


class DbEngineDirectory:
    """Engines registered in the ``service_engine`` table, reached through the Data Plane API."""

    def __init__(
        self,
        db: Databases,
        secrets: SecretsReader,
        tls: DataPlaneTls | None = None,
        signer: BundleSigner | None = None,
        agent_tls: DataPlaneTls | None = None,
    ) -> None:
        """Create the directory.

        Args:
            db: Databases (tenant-scoped reads).
            secrets: Resolves each engine's ``credentials_ref``.
            tls: Controller-side TLS settings for the Data Plane API connections.
            signer: Signs agent bundles; without it engines are treated as having no agent.
            agent_tls: TLS settings for agent connections (defaults to ``tls``).
        """
        self._db = db
        self._secrets = secrets
        self._tls = tls or DataPlaneTls()
        self._signer = signer
        self._agent_tls = agent_tls or self._tls

    async def list_engines(self, tenant_id: UUID, se_group_id: UUID) -> list[EngineRecord]:
        """Return the group's engines sorted by name."""
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = (
                await session.execute(
                    select(ServiceEngineRow)
                    .where(ServiceEngineRow.se_group_id == se_group_id)
                    .order_by(ServiceEngineRow.name)
                )
            ).scalars()
            return [
                EngineRecord(
                    id=r.id,
                    tenant_id=r.tenant_id,
                    se_group_id=r.se_group_id,
                    name=r.name,
                    dataplane_url=r.dataplane_url,
                    credentials_ref=r.credentials_ref,
                    state=EngineState(r.state),
                    agent_url=r.agent_url,
                    mgmt_ip=str(r.mgmt_ip).split("/")[0] if r.mgmt_ip else None,
                )
                for r in rows
            ]

    async def connect(self, record: EngineRecord) -> HaproxyEngine:
        """Create a Data Plane API client for ``record``."""
        username = password = None
        if record.credentials_ref:
            username, _, password = (await self._secrets.get(record.credentials_ref)).partition(":")
        return DataPlaneEngine(
            record.dataplane_url,
            name=record.name,
            username=username,
            password=password,
            ca_file=self._tls.ca_file,
            client_cert=self._tls.client_cert,
        )

    async def connect_agent(self, record: EngineRecord) -> AgentEngine | None:
        """Open a gRPC client to the engine's agent, if it has one and a signer is configured."""
        if not record.agent_url or self._signer is None:
            return None
        return GrpcAgent(
            record.agent_url,
            name=record.name,
            signer=self._signer,
            ca_file=self._agent_tls.ca_file,
            client_cert=self._agent_tls.client_cert,
        )


class StaticEngineDirectory:
    """A fixed set of engines, for tests: ``se_group_id -> [(record, engine)]``."""

    def __init__(
        self,
        engines: Mapping[UUID, Sequence[tuple[EngineRecord, HaproxyEngine]]],
        agents: Mapping[UUID, AgentEngine] | None = None,
    ) -> None:
        """Create the directory from ready-made engines (and agents, keyed by engine id)."""
        self._engines = {k: list(v) for k, v in engines.items()}
        self._agents = dict(agents or {})

    async def connect_agent(self, record: EngineRecord) -> AgentEngine | None:
        """Return the prepared agent for ``record`` if there is one."""
        return self._agents.get(record.id)

    async def list_engines(self, tenant_id: UUID, se_group_id: UUID) -> list[EngineRecord]:
        """Return the configured records sorted by name."""
        return sorted((r for r, _ in self._engines.get(se_group_id, [])), key=lambda r: r.name)

    async def connect(self, record: EngineRecord) -> HaproxyEngine:
        """Return the prepared engine for ``record``."""
        for candidate, engine in self._engines[record.se_group_id]:
            if candidate.id == record.id:
                return engine
        raise KeyError(record.id)


EngineFactory = Callable[[EngineRecord], HaproxyEngine]
