"""Infrastructure jobs: SDN for managed networks, and SE group deployment on Proxmox.

Both run in workers (never in an HTTP request). SDN apply is cluster-wide in Proxmox, so every SDN
change is serialized behind one advisory lock. A deployment holds the SE group's lock, the same one
the apply pipeline uses, so a deploy and a config push never interleave.

Deployment creates VMs from the SE template, gives each a management address from an IPAM pool,
starts them and registers them as ``service_engine`` rows. If any step fails, the VMs created by
that run are destroyed and their rows removed; engines that already existed are never touched.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from itertools import cycle
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select

from switchyard.adapters.proxmox import Proxmox, ProxmoxError, SdnSubnet, VmSpec
from switchyard.db import audit_log
from switchyard.db.models import TenantRow
from switchyard.db.ops_models import JobRow, SdnObjectRow, ServiceEngineRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import ipam
from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot
from switchyard.domain.validators import check_secret_ref_scope
from switchyard.ids import uuid7
from switchyard.jobs import queue
from switchyard.pipeline.locks import PgGroupLocks
from switchyard.services.changesets import require
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import PermissionDeniedError

log = logging.getLogger("switchyard.infra")

APPLY_SDN_JOB = "apply_sdn"
RETIRE_ENGINE_JOB = "retire_engine"
DEPLOY_SE_GROUP_JOB = "deploy_se_group"

#: Stand-in "group id" for the cluster-wide SDN lock.
SDN_LOCK_ID = UUID("00000000-0000-7000-8000-5344e0000001")


class DeployError(Exception):
    """A deployment cannot proceed or failed (and was cleaned up)."""


@dataclass(frozen=True)
class DeployRequest:
    """Parameters of ``deploy_se_group``.

    Attributes:
        tenant_id: Owner.
        se_group_id: The group to grow.
        count: Total number of engines the group should have afterwards.
        nodes: Proxmox nodes to spread new VMs over, round-robin.
        template_vmid: The SE template to clone.
        mgmt_pool_id: A pool on the group's management network that management addresses come from.
        pool: Proxmox resource pool for the VMs, if any.
        credentials_ref: Secret reference for the Data Plane API credentials of the new engines.
        dataplane_port: Data Plane API port.
        agent_port: ``sy-agent`` gRPC port.
        ssh_public_keys: Operator keys for cloud-init (or the container's authorized keys).
        guest_kind: ``qemu`` (cloud-init VMs, the default) or ``lxc`` (containers: lighter, no
            cloud-init, so enrollment material is delivered out of band; ADR 0054).
    """

    tenant_id: UUID
    se_group_id: UUID
    count: int
    nodes: tuple[str, ...]
    template_vmid: int
    mgmt_pool_id: UUID
    pool: str | None = None
    credentials_ref: str | None = None
    dataplane_port: int = 5556
    agent_port: int = 7443
    ssh_public_keys: str | None = None
    guest_kind: str = "qemu"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DeployRequest":
        """Build a request from a job payload."""
        return cls(
            tenant_id=UUID(payload["tenant_id"]),
            se_group_id=UUID(payload["se_group_id"]),
            count=int(payload["count"]),
            nodes=tuple(payload["nodes"]),
            template_vmid=int(payload["template_vmid"]),
            mgmt_pool_id=UUID(payload["mgmt_pool_id"]),
            pool=payload.get("pool"),
            credentials_ref=payload.get("credentials_ref"),
            dataplane_port=int(payload.get("dataplane_port", 5556)),
            agent_port=int(payload.get("agent_port", 7443)),
            ssh_public_keys=payload.get("ssh_public_keys"),
            guest_kind=str(payload.get("guest_kind", "qemu")),
        )

    def as_payload(self) -> dict[str, Any]:
        """Return the job payload."""
        out: dict[str, Any] = {
            "tenant_id": str(self.tenant_id),
            "se_group_id": str(self.se_group_id),
            "count": self.count,
            "nodes": list(self.nodes),
            "template_vmid": self.template_vmid,
            "mgmt_pool_id": str(self.mgmt_pool_id),
            "dataplane_port": self.dataplane_port,
            "agent_port": self.agent_port,
            "guest_kind": self.guest_kind,
        }
        for key in ("pool", "credentials_ref", "ssh_public_keys"):
            if getattr(self, key) is not None:
                out[key] = getattr(self, key)
        return out


@dataclass(frozen=True)
class SdnApplyState:
    """Where the Proxmox SDN apply stands for one managed network.

    Attributes:
        network_id: The network.
        network: Its name.
        state: ``queued`` or ``running`` (a tenant-wide apply is in flight and will touch it),
            ``succeeded`` (the latest finished apply included it), ``failed`` (the latest
            finished apply failed) or ``never`` (no apply has included it yet).
        job_id: The job the state comes from.
        started_at: When that job started.
        finished_at: When that job finished.
        error: Error of a failed job.
    """

    network_id: UUID
    network: str
    state: str
    job_id: UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class InfraJobs:
    """Queues infrastructure jobs for a tenant administrator (the API side)."""

    def __init__(self, db: Databases) -> None:
        """Create the service."""
        self._db = db

    async def sdn_status(self, principal: Principal, tenant_id: UUID) -> list[SdnApplyState]:
        """The SDN apply state of every managed network, by name.

        An apply is tenant-wide, so a queued or running job counts for every managed network. Once
        it has finished the answer is the latest finished job: ``failed`` for all, or ``succeeded``
        for the networks the job reported and ``never`` for networks created after it.
        """
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            networks = sorted(
                (n for n in (await load_snapshot(session)).networks if n.managed),
                key=lambda n: n.name,
            )
        async with admin_session(self._db.admin) as session:
            jobs = (
                (
                    await session.execute(
                        select(JobRow)
                        .where(JobRow.tenant_id == tenant_id, JobRow.kind == APPLY_SDN_JOB)
                        .order_by(JobRow.created_at.desc())
                        .limit(10)
                    )
                )
                .scalars()
                .all()
            )
        in_flight = next((j for j in jobs if j.state in {"queued", "running"}), None)
        finished = next((j for j in jobs if j.state in {"succeeded", "failed", "dead"}), None)
        out: list[SdnApplyState] = []
        for net in networks:
            job, state, error = in_flight, in_flight.state if in_flight else "never", None
            if in_flight is None and finished is not None:
                job, error = finished, finished.last_error
                if finished.state == "succeeded":
                    done = (finished.result or {}).get("networks", [])
                    state = "succeeded" if net.name in done else "never"
                else:
                    state = "failed"
                if state == "never":
                    job, error = None, None
            out.append(
                SdnApplyState(
                    network_id=net.id,
                    network=net.name,
                    state=state,
                    job_id=job.id if job else None,
                    started_at=job.started_at if job else None,
                    finished_at=job.finished_at if job else None,
                    error=error,
                )
            )
        return out

    async def request_sdn_apply(
        self, principal: Principal, tenant_id: UUID, *, request_id: str | None = None
    ) -> UUID:
        """Queue ``apply_sdn`` and return the job id."""
        self._check(principal, tenant_id, "apply", "sdn")
        async with admin_session(self._db.admin) as session:
            return await queue.enqueue(
                session,
                APPLY_SDN_JOB,
                {"tenant_id": str(tenant_id)},
                tenant_id=tenant_id,
                max_attempts=1,
                request_id=request_id,
            )

    async def request_deploy(
        self,
        principal: Principal,
        tenant_id: UUID,
        request: "DeployRequest",
        *,
        request_id: str | None = None,
    ) -> UUID:
        """Queue ``deploy_se_group`` and return the job id."""
        self._check(principal, tenant_id, "deploy", "se_group")
        if request.tenant_id != tenant_id:
            raise PermissionDeniedError("request is for another tenant")
        if request.credentials_ref:
            async with admin_session(self._db.admin) as session:
                tenant_name = (
                    await session.execute(select(TenantRow.name).where(TenantRow.id == tenant_id))
                ).scalar_one()
            if why := check_secret_ref_scope(request.credentials_ref, tenant_name):
                raise ValueError(f"credentials_ref: {why}")
        async with admin_session(self._db.admin) as session:
            return await queue.enqueue(
                session,
                DEPLOY_SE_GROUP_JOB,
                request.as_payload(),
                tenant_id=tenant_id,
                max_attempts=1,
                request_id=request_id,
            )

    async def request_retire(
        self,
        principal: Principal,
        tenant_id: UUID,
        engine_id: UUID,
        *,
        request_id: str | None = None,
    ) -> UUID:
        """Queue the removal of an engine and its VM and return the job id."""
        self._check(principal, tenant_id, "manage", "engine")
        async with admin_session(self._db.admin) as session:
            return await queue.enqueue(
                session,
                RETIRE_ENGINE_JOB,
                {"tenant_id": str(tenant_id), "engine_id": str(engine_id)},
                tenant_id=tenant_id,
                max_attempts=1,
                request_id=request_id,
            )

    @staticmethod
    def _check(principal: Principal, tenant_id: UUID, verb: str, obj: str) -> None:
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        require(principal, verb, obj)


class InfraService:
    """SDN and deployment jobs against a :class:`~switchyard.adapters.proxmox.Proxmox`."""

    def __init__(self, db: Databases, proxmox: Proxmox, *, lock_timeout_s: float = 300.0) -> None:
        """Create the service."""
        self._db = db
        self._pve = proxmox
        self._locks = PgGroupLocks(db.admin, timeout_s=lock_timeout_s)

    async def _snapshot(self, tenant_id: UUID) -> Snapshot:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            return await load_snapshot(session)

    async def apply_sdn(self, tenant_id: UUID) -> dict[str, Any]:
        """Reconcile Proxmox SDN with the tenant's managed networks, then apply.

        Creates the zones, VNets and subnets of managed networks and records the zones and VNets
        this call created. Then deletes recorded VNets (and zones) that no managed network uses
        any more. Objects that existed before, and attached (``managed: false``) networks, are
        never touched. Idempotent.

        Returns:
            ``{"networks": [...], "removed": ["vnet:x", "zone:y", ...]}``.
        """
        snapshot = await self._snapshot(tenant_id)
        managed = [n for n in snapshot.networks if n.managed and n.sdn_zone and n.vnet]
        wanted_vnets = {n.vnet for n in managed}
        wanted_zones = {n.sdn_zone for n in managed}
        done: list[str] = []
        removed: list[str] = []
        async with self._locks.hold([SDN_LOCK_ID]):
            for net in sorted(managed, key=lambda n: n.name):
                if net.sdn_zone is None or net.vnet is None:
                    continue
                if await self._pve.ensure_zone(net.sdn_zone):
                    await self._record(tenant_id, "zone", net.sdn_zone)
                if await self._pve.ensure_vnet(net.vnet, net.sdn_zone, tag=net.vlan_or_vni):
                    await self._record(tenant_id, "vnet", net.vnet)
                gateway = str(net.gateway) if net.gateway else None
                await self._pve.ensure_subnet(net.vnet, SdnSubnet(str(net.cidr), gateway))
                done.append(net.name)
            recorded = await self._recorded(tenant_id)
            for kind, name in sorted(recorded, key=lambda r: (r[0] != "vnet", r[1])):
                if kind == "vnet" and name not in wanted_vnets:
                    await self._pve.delete_vnet(name)
                    await self._forget(tenant_id, kind, name)
                    removed.append(f"vnet:{name}")
            for kind, name in sorted(recorded):
                if kind == "zone" and name not in wanted_zones:
                    if await self._pve.zone_vnets(name):
                        continue  # someone else's VNet lives there: leave the zone alone
                    await self._pve.delete_zone(name)
                    await self._forget(tenant_id, kind, name)
                    removed.append(f"zone:{name}")
            if done or removed:
                await self._pve.apply_sdn()
        return {"networks": done, "removed": removed}

    async def _record(self, tenant_id: UUID, kind: str, name: str) -> None:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            if await session.get(SdnObjectRow, (tenant_id, kind, name)) is None:
                session.add(SdnObjectRow(tenant_id=tenant_id, kind=kind, name=name))

    async def _recorded(self, tenant_id: UUID) -> list[tuple[str, str]]:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            rows = (await session.execute(select(SdnObjectRow))).scalars().all()
            return [(r.kind, r.name) for r in rows]

    async def _forget(self, tenant_id: UUID, kind: str, name: str) -> None:
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = await session.get(SdnObjectRow, (tenant_id, kind, name))
            if row is not None:
                await session.delete(row)

    async def deploy_se_group(self, request: DeployRequest) -> dict[str, Any]:
        """Grow an SE group to ``request.count`` engines.

        Returns:
            ``{"created": [...], "existing": [...]}`` with engine names.

        Raises:
            DeployError: If the request cannot be satisfied or a step failed (the VMs this call
                created are removed first).
        """
        if request.count < 1 or not request.nodes:
            raise DeployError("count must be at least 1 and at least one node is required")
        snapshot = await self._snapshot(request.tenant_id)
        group = next((g for g in snapshot.se_groups if g.id == request.se_group_id), None)
        if group is None:
            raise DeployError(f"SE group {request.se_group_id} does not exist")
        networks = {n.id: n for n in snapshot.networks}
        mgmt_links = [link for link in group.networks if link.role is dm.NetworkRole.MGMT]
        if len(mgmt_links) != 1:
            raise DeployError(f"SE group {group.name} needs exactly one mgmt network")
        mgmt = networks[mgmt_links[0].network_id]
        pool = next((p for p in snapshot.vip_pools if p.id == request.mgmt_pool_id), None)
        if pool is None or pool.network_id != mgmt.id:
            raise DeployError("mgmt_pool_id must be a pool on the group's mgmt network")
        extra: list[tuple[str, int | None]] = []
        for link in group.networks:
            if link.role is dm.NetworkRole.MGMT:
                continue
            net = networks[link.network_id]
            if not net.vnet:
                raise DeployError(f"network {net.name} has no VNet to attach VMs to")
            extra.append((net.vnet, None))
        if not mgmt.vnet:
            raise DeployError(f"management network {mgmt.name} has no VNet")

        created: list[tuple[str, int, UUID]] = []  # node, vmid, engine row id
        names: list[str] = []
        existing: list[str] = []
        try:
            async with self._locks.hold([request.se_group_id]):
                async with tenant_session(self._db.app, [request.tenant_id]) as session:
                    rows = (
                        (
                            await session.execute(
                                select(ServiceEngineRow).where(
                                    ServiceEngineRow.se_group_id == request.se_group_id
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    used = [ipam_address(r.mgmt_ip) for r in rows if r.mgmt_ip]
                    existing = sorted(r.name for r in rows)
                    taken_names = set(existing)
                nodes = cycle(request.nodes)
                number = 0
                while len(taken_names) + len(names) < request.count:
                    number += 1
                    name = f"{group.name}-{number}"
                    if name in taken_names:
                        continue
                    address = ipam.allocate(pool, mgmt, used)
                    used.append(address)
                    spec = VmSpec(
                        name=name,
                        node=next(nodes),
                        template_vmid=request.template_vmid,
                        mgmt_ip_cidr=f"{address}/{mgmt.cidr.prefixlen}",
                        gateway=str(mgmt.gateway) if mgmt.gateway else None,
                        pool=request.pool,
                        mgmt_bridge=mgmt.vnet,
                        nics=tuple(extra),
                        ssh_public_keys=request.ssh_public_keys,
                        kind=request.guest_kind,
                    )
                    info = await self._pve.create_vm(spec)
                    created.append((info.node, info.vmid, UUID(int=0)))
                    await self._pve.start_vm(info.node, info.vmid)
                    row_id = await self._register(request, name, str(address), info.node, info.vmid)
                    created[-1] = (info.node, info.vmid, row_id)
                    names.append(name)
        except (ProxmoxError, ipam.PoolExhaustedError) as exc:
            await self._cleanup(request.tenant_id, created)
            raise DeployError(f"deploy of {group.name} failed and was cleaned up: {exc}") from exc
        return {"created": names, "existing": existing}

    async def retire_engine(self, tenant_id: UUID, engine_id: UUID) -> dict[str, Any]:
        """Take an engine out of service and destroy its VM.

        The engine is disabled first (so no rollout includes it), then the VM is destroyed and the
        row removed. The last enabled engine of a group that serves enabled virtual services cannot
        be retired: that would stop the service.

        Raises:
            DeployError: If the engine does not exist or is the last one serving traffic.
        """
        snapshot = await self._snapshot(tenant_id)
        async with tenant_session(self._db.app, [tenant_id]) as session:
            row = (
                await session.execute(
                    select(ServiceEngineRow)
                    .where(ServiceEngineRow.id == engine_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise DeployError(f"engine {engine_id} does not exist")
            siblings = (
                (
                    await session.execute(
                        select(ServiceEngineRow).where(
                            ServiceEngineRow.se_group_id == row.se_group_id,
                            ServiceEngineRow.id != row.id,
                            ServiceEngineRow.state == "enabled",
                        )
                    )
                )
                .scalars()
                .all()
            )
            serving = any(
                s.se_group_id == row.se_group_id and s.enabled for s in snapshot.virtual_services
            )
            if serving and not siblings and row.state == "enabled":
                raise DeployError(
                    f"{row.name} is the only enabled engine of a group that serves traffic"
                )
            row.state = "disabled"
            node, vmid, name = row.pve_node, row.vmid, row.name
        if node and vmid:
            try:
                await self._pve.destroy_vm(node, vmid)
            except ProxmoxError as exc:
                raise DeployError(f"could not destroy the VM of {name}: {exc}") from exc
        async with tenant_session(self._db.app, [tenant_id]) as session:
            await session.execute(delete(ServiceEngineRow).where(ServiceEngineRow.id == engine_id))
            await audit_log.record(
                session,
                tenant_id=tenant_id,
                actor="system:deploy",
                action="engine.retired",
                object_ref=f"engine:{engine_id}",
                diff={"name": name, "vmid": vmid},
            )
        return {"retired": name, "vm_destroyed": bool(node and vmid)}

    async def _register(
        self, request: DeployRequest, name: str, address: str, node: str, vmid: int
    ) -> UUID:
        async with tenant_session(self._db.app, [request.tenant_id]) as session:
            row = ServiceEngineRow(
                id=uuid7(),
                tenant_id=request.tenant_id,
                se_group_id=request.se_group_id,
                name=name,
                mgmt_ip=address,
                dataplane_url=f"https://{address}:{request.dataplane_port}",
                credentials_ref=request.credentials_ref,
                agent_url=f"{address}:{request.agent_port}",
                pve_node=node,
                vmid=vmid,
            )
            session.add(row)
            await session.flush()
            await audit_log.record(
                session,
                tenant_id=request.tenant_id,
                actor="system:deploy",
                action="engine.deployed",
                object_ref=f"engine:{row.id}",
                diff={"name": name, "node": node, "vmid": vmid, "mgmt_ip": address},
            )
            return row.id

    async def _cleanup(self, tenant_id: UUID, created: list[tuple[str, int, UUID]]) -> None:
        """Remove the VMs and rows a failed deploy created. Never raises."""
        for node, vmid, row_id in reversed(created):
            try:
                await self._pve.destroy_vm(node, vmid)
            except ProxmoxError as exc:
                log.error("cleanup of VM %s on %s failed: %s", vmid, node, exc)
            async with tenant_session(self._db.app, [tenant_id]) as session:
                await session.execute(delete(ServiceEngineRow).where(ServiceEngineRow.id == row_id))


def ipam_address(value: object) -> dm.IPAddress:
    """Parse an INET column value (possibly with a prefix length) into an address."""
    import ipaddress

    return ipaddress.ip_address(str(value).split("/")[0])
