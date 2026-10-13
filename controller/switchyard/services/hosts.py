# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Proxmox hosts: online state, load and where an SE group's engines run.

The API reads the cluster through the :class:`~switchyard.adapters.proxmox.Proxmox` adapter (read
calls only). Answers are cached for a few seconds so a page that lists engines and placements does
not call Proxmox once per request. When Proxmox is not configured or cannot be reached, the answer
says so (``available: false``) and still lists the nodes the engines claim to run on, with an
unknown (``None``) online state, so the UI can show the layout without pretending to know the state.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select

from switchyard.adapters.proxmox import NodeInfo, Proxmox, ProxmoxError, StorageInfo
from switchyard.db.models import SeGroupRow
from switchyard.db.tenancy import tenant_session
from switchyard.services.context import Databases, Principal
from switchyard.services.engines import EngineService
from switchyard.services.errors import NotFoundError, PermissionDeniedError

log = logging.getLogger("switchyard.hosts")

Clock = Callable[[], float]


@dataclass(frozen=True)
class HostStorage:
    """A storage of a node, sizes in bytes."""

    name: str
    kind: str
    active: bool
    content: tuple[str, ...]
    total: int | None
    used: int | None
    available: int | None


@dataclass(frozen=True)
class HostEngine:
    """An engine of the SE group that runs on a node."""

    id: UUID
    name: str
    vmid: int | None
    state: str


@dataclass(frozen=True)
class HostNode:
    """One Proxmox node and the group's engines on it.

    Attributes:
        name: Node name.
        online: ``True`` or ``False`` as the cluster reports; ``None`` when Proxmox could not be
            asked.
        cpu: CPU load as a fraction (0 to 1).
        max_cpu: Number of CPUs.
        mem_used: Memory in use, bytes.
        mem_total: Memory installed, bytes.
        uptime_s: Seconds since boot.
        storage: The node's storages (empty when unknown).
        engines: The group's engines that run on this node.
    """

    name: str
    online: bool | None
    cpu: float | None
    max_cpu: int | None
    mem_used: int | None
    mem_total: int | None
    uptime_s: int | None
    storage: tuple[HostStorage, ...]
    engines: tuple[HostEngine, ...]


@dataclass(frozen=True)
class Placement:
    """Where an SE group's engines run.

    Attributes:
        available: ``False`` when Proxmox is not configured or could not be read; node states are
            then unknown.
        reason: Why it is not available.
        nodes: Every cluster node (or, without Proxmox, every node an engine is placed on).
    """

    available: bool
    reason: str | None
    nodes: tuple[HostNode, ...]


class HostService:
    """Read-only view of the Proxmox cluster for the API."""

    def __init__(
        self,
        db: Databases,
        engines: EngineService,
        proxmox: Proxmox | None,
        *,
        ttl_s: float = 15.0,
        clock: Clock = time.monotonic,
    ) -> None:
        """Create the service. ``proxmox`` is ``None`` when the cluster is not configured."""
        self._db = db
        self._engines = engines
        self._pve = proxmox
        self._ttl = ttl_s
        self._clock = clock
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def _cached(self, key: str, fetch: Callable[[], Awaitable[Any]]) -> Any:
        async with self._lock:
            hit = self._cache.get(key)
            if hit is not None and self._clock() - hit[0] < self._ttl:
                return hit[1]
            value = await fetch()
            self._cache[key] = (self._clock(), value)
            return value

    async def _nodes(self) -> list[NodeInfo] | None:
        """The cluster nodes, or ``None`` when Proxmox is missing or failing."""
        if self._pve is None:
            return None
        pve = self._pve
        try:
            result: list[NodeInfo] = await self._cached("nodes", pve.list_nodes)
        except ProxmoxError as exc:
            log.warning("reading the Proxmox nodes failed: %s", exc)
            return None
        return result

    async def _storage(self, node: str) -> tuple[StorageInfo, ...]:
        if self._pve is None:
            return ()
        pve = self._pve

        async def fetch() -> tuple[StorageInfo, ...]:
            return tuple(await pve.list_storage(node))

        try:
            result: tuple[StorageInfo, ...] = await self._cached(f"storage:{node}", fetch)
        except ProxmoxError as exc:
            log.warning("reading the storage of node %s failed: %s", node, exc)
            return ()
        return result

    async def online_by_node(self) -> dict[str, bool]:
        """Node name to online state; empty when Proxmox is not configured or not reachable."""
        return {n.name: n.online for n in await self._nodes() or []}

    async def placement(
        self, principal: Principal, tenant_id: UUID, se_group_id: UUID
    ) -> Placement:
        """Describe the cluster nodes and which of the group's engines run on each.

        Raises:
            PermissionDeniedError: If the principal has no access to the tenant.
            NotFoundError: If the SE group does not exist in the tenant.
        """
        if not principal.can_access(tenant_id):
            raise PermissionDeniedError(f"{principal.actor} has no access to this tenant")
        async with tenant_session(self._db.app, [tenant_id]) as session:
            found = (
                await session.execute(select(SeGroupRow.id).where(SeGroupRow.id == se_group_id))
            ).first()
        if found is None:
            raise NotFoundError(f"SE group {se_group_id} not found")
        by_node: dict[str, list[HostEngine]] = {}
        for d in await self._engines.describe(principal, tenant_id, se_group_id=se_group_id):
            if d.pve_node:
                by_node.setdefault(d.pve_node, []).append(
                    HostEngine(d.record.id, d.record.name, d.vmid, d.record.state.value)
                )
        cluster = await self._nodes()
        if cluster is None:
            reason = (
                "Proxmox is not configured" if self._pve is None else "Proxmox could not be reached"
            )
            return Placement(
                False,
                reason,
                tuple(
                    HostNode(name, None, None, None, None, None, None, (), tuple(engines))
                    for name, engines in sorted(by_node.items())
                ),
            )
        nodes = []
        known = {n.name for n in cluster}
        for n in cluster:
            storage = await self._storage(n.name) if n.online else ()
            nodes.append(
                HostNode(
                    name=n.name,
                    online=n.online,
                    cpu=n.cpu,
                    max_cpu=n.max_cpu,
                    mem_used=n.mem_used,
                    mem_total=n.mem_total,
                    uptime_s=n.uptime_s,
                    storage=tuple(
                        HostStorage(
                            s.name, s.kind, s.active, s.content, s.total, s.used, s.available
                        )
                        for s in storage
                    ),
                    engines=tuple(by_node.get(n.name, ())),
                )
            )
        # An engine on a node the cluster no longer lists is shown, as offline.
        for name in sorted(set(by_node) - known):
            nodes.append(
                HostNode(name, False, None, None, None, None, None, (), tuple(by_node[name]))
            )
        return Placement(True, None, tuple(nodes))
