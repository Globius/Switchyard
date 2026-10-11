"""Register the service engines (SE VMs) of an SE group.

M2 registers engines by hand ("a manually built SE pair"); from M4 the Proxmox adapter creates and
registers them. An engine row says where its Data Plane API is and which secret holds its
credentials; the controller never stores the credentials themselves.
"""

from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import delete, select

from switchyard.db import audit_log
from switchyard.db.models import SeGroupRow, TenantRow
from switchyard.db.ops_models import ServiceEngineRow
from switchyard.db.tenancy import tenant_session
from switchyard.domain.validators import (
    check_secret_ref_scope,
    validate_name,
    validate_secret_ref,
)
from switchyard.domain.workflow import EngineState
from switchyard.ids import uuid7
from switchyard.pipeline.engines import EngineRecord
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import AlreadyExistsError, NotFoundError, PermissionDeniedError


def validate_dataplane_url(url: str) -> str:
    """Accept ``http(s)://host[:port]`` without a path, credentials or query.

    Example:
        >>> validate_dataplane_url("https://se-1.mgmt.example.test:5556")
        'https://se-1.mgmt.example.test:5556'
        >>> validate_dataplane_url("ftp://x")
        Traceback (most recent call last):
            ...
        ValueError: dataplane_url must be http(s)://host[:port]
    """
    parsed = urlparse(url)
    ok = (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.path in {"", "/"}
        and not parsed.username
        and not parsed.query
        and not parsed.fragment
    )
    if not ok:
        raise ValueError("dataplane_url must be http(s)://host[:port]")
    return f"{parsed.scheme}://{parsed.netloc}"


@dataclass(frozen=True)
class EngineDetail:
    """An engine with its placement and enrollment state, as the API shows it.

    Attributes:
        record: The registration (where its Data Plane API and agent are).
        pve_node: Proxmox node the VM runs on (``None`` for a hand-built engine).
        vmid: Proxmox VM id (``None`` for a hand-built engine).
        agent_cert_serial: Serial of the agent's client certificate once it has enrolled.
    """

    record: EngineRecord
    pve_node: str | None = None
    vmid: int | None = None
    agent_cert_serial: str | None = None


def _record(row: ServiceEngineRow) -> EngineRecord:
    return EngineRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        se_group_id=row.se_group_id,
        name=row.name,
        dataplane_url=row.dataplane_url,
        credentials_ref=row.credentials_ref,
        state=EngineState(row.state),
        agent_url=row.agent_url,
        mgmt_ip=str(row.mgmt_ip).split("/")[0] if row.mgmt_ip else None,
    )


class EngineService:
    """Creates, lists, enables/disables and removes service engines."""

    def __init__(self, db: Databases) -> None:
        """Create the service."""
        self._db = db

    def _check(self, principal: Principal, tenant_id: UUID) -> None:
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, "manage", "engine")

    async def register(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        se_group_id: UUID,
        name: str,
        dataplane_url: str,
        credentials_ref: str | None = None,
        mgmt_ip: str | None = None,
        agent_url: str | None = None,
        request_id: str | None = None,
    ) -> EngineRecord:
        """Add an engine to an SE group.

        Raises:
            ValueError: If the name, URL or secret reference is malformed.
            NotFoundError: If the SE group does not exist.
            AlreadyExistsError: If the group already has an engine with that name.
        """
        self._check(principal, tenant_id)
        validate_name(name)
        url = validate_dataplane_url(dataplane_url)
        if credentials_ref is not None:
            validate_secret_ref(credentials_ref)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            if credentials_ref is not None:
                tenant_name = (
                    await session.execute(select(TenantRow.name).where(TenantRow.id == tenant_id))
                ).scalar_one()
                if why := check_secret_ref_scope(credentials_ref, tenant_name):
                    raise ValueError(f"credentials_ref: {why}")
            group = (
                await session.execute(
                    select(ServiceEngineRow.id).where(
                        ServiceEngineRow.se_group_id == se_group_id,
                        ServiceEngineRow.name == name,
                    )
                )
            ).first()
            if group is not None:
                raise AlreadyExistsError(f"engine {name!r} already exists in this SE group")
            if (
                await session.execute(select(SeGroupRow.id).where(SeGroupRow.id == se_group_id))
            ).first() is None:
                raise NotFoundError(f"SE group {se_group_id} does not exist")
            row = ServiceEngineRow(
                id=uuid7(),
                tenant_id=tenant_id,
                se_group_id=se_group_id,
                name=name,
                dataplane_url=url,
                credentials_ref=credentials_ref,
                agent_url=agent_url,
                mgmt_ip=mgmt_ip,
            )
            session.add(row)
            await session.flush()
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="engine.register",
                object_ref=f"engine:{row.id}",
                diff={"name": name, "se_group_id": str(se_group_id), "url": url},
                request_id=request_id,
            )
            return _record(row)

    async def list_engines(
        self, principal: Principal, tenant_id: UUID, se_group_id: UUID | None = None
    ) -> list[EngineRecord]:
        """List engines, optionally of one SE group."""
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            query = select(ServiceEngineRow).order_by(
                ServiceEngineRow.se_group_id, ServiceEngineRow.name
            )
            if se_group_id:
                query = query.where(ServiceEngineRow.se_group_id == se_group_id)
            return [_record(r) for r in (await session.execute(query)).scalars()]

    async def describe(
        self,
        principal: Principal,
        tenant_id: UUID,
        *,
        se_group_id: UUID | None = None,
        state: EngineState | None = None,
        engine_id: UUID | None = None,
    ) -> list[EngineDetail]:
        """List engines with placement and enrollment, filtered by group, state or id."""
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            query = select(ServiceEngineRow).order_by(
                ServiceEngineRow.se_group_id, ServiceEngineRow.name
            )
            if se_group_id:
                query = query.where(ServiceEngineRow.se_group_id == se_group_id)
            if state:
                query = query.where(ServiceEngineRow.state == state.value)
            if engine_id:
                query = query.where(ServiceEngineRow.id == engine_id)
            return [
                EngineDetail(_record(r), r.pve_node, r.vmid, r.agent_cert_serial)
                for r in (await session.execute(query)).scalars()
            ]

    async def set_state(
        self,
        principal: Principal,
        tenant_id: UUID,
        engine_id: UUID,
        state: EngineState,
        *,
        request_id: str | None = None,
    ) -> EngineRecord:
        """Enable or disable an engine for rollouts."""
        self._check(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = (
                await session.execute(
                    select(ServiceEngineRow)
                    .where(ServiceEngineRow.id == engine_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise NotFoundError(f"engine {engine_id} does not exist")
            row.state = state.value
            row.version += 1
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action=f"engine.{state.value}",
                object_ref=f"engine:{engine_id}",
                request_id=request_id,
            )
            return _record(row)

    async def remove(
        self,
        principal: Principal,
        tenant_id: UUID,
        engine_id: UUID,
        *,
        request_id: str | None = None,
    ) -> None:
        """Remove an engine row (the VM is not touched)."""
        self._check(principal, tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            result = await session.execute(
                delete(ServiceEngineRow).where(ServiceEngineRow.id == engine_id)
            )
            if result.rowcount == 0:  # type: ignore[attr-defined]
                raise NotFoundError(f"engine {engine_id} does not exist")
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor=principal.actor,
                action="engine.remove",
                object_ref=f"engine:{engine_id}",
                request_id=request_id,
            )
