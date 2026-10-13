# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Pool discovery: keep a pool's members in step with the Proxmox guests that carry its tag.

``discover_pool_members`` runs every minute. For each pool with a ``discovery`` block it lists the
running guests that carry the pool's tag, asks each for its addresses (QEMU guest agent, or the
container's interface list) and takes the first address inside the discovery CIDR. Differences go
into one ordinary change set per tenant, opened by ``system:discovery`` and applied through the
normal pipeline (canary, revision, Git mirror). Where a tenant requires approval the change set
waits for it, like any other automatic change.

Rules that keep it safe:

- A member's id is derived from the pool id and the guest id, so it is stable across runs.
  Weight and admin state an operator set on a discovered member are kept.
- A running guest whose addresses cannot be read (no agent yet) keeps its current member and is
  not added if new. A guest that disappears or stops loses its member.
- A run never empties a pool: if the result would have no members while the pool has some, the
  pool is left alone and a warning is logged (Proxmox may simply be unreachable or mid-migration).
- One discovery change set at a time per tenant: while one is a draft, waiting for approval or
  applying, the tenant is skipped.

Example:
    >>> import ipaddress
    >>> pick_address(("fe80::1", "10.0.0.7", "10.9.9.9"), ipaddress.ip_network("10.0.0.0/24"))
    '10.0.0.7'
"""

import ipaddress
import logging
from collections.abc import Sequence
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import select

from switchyard.adapters.proxmox import GuestInfo, Proxmox, ProxmoxError
from switchyard.db.models import TenantRow
from switchyard.db.repository import load_snapshot
from switchyard.db.tenancy import admin_session, tenant_session
from switchyard.domain import models as dm
from switchyard.domain.changes import Action, ChangeOp, ObjectType
from switchyard.domain.workflow import ChangeSetState, Role
from switchyard.services.changesets import ChangeSetService
from switchyard.services.context import Databases, Principal
from switchyard.services.errors import PermissionDeniedError

log = logging.getLogger("switchyard.discovery")

DISCOVER_POOLS_JOB = "discover_pool_members"
ACTOR = "system:discovery"
#: Namespace of discovered member ids (``uuid5(namespace, "<pool id>:<vmid>")``).
MEMBER_NAMESPACE = UUID("5a5e7d6c-0000-4000-8000-00000000d15c")

_OPEN_STATES = {
    ChangeSetState.DRAFT,
    ChangeSetState.PENDING_APPROVAL,
    ChangeSetState.APPLYING,
}


def pick_address(addresses: Sequence[str], network: dm.IPNetwork) -> str | None:
    """Return the first of ``addresses`` that lies in ``network``.

    Example:
        >>> import ipaddress
        >>> pick_address(("10.0.0.7",), ipaddress.ip_network("192.0.2.0/24")) is None
        True
    """
    for text in addresses:
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        if address.version == network.version and address in network:
            return str(address)
    return None


def member_id(pool_id: UUID, vmid: int) -> UUID:
    """The stable member id for guest ``vmid`` in a pool.

    Example:
        >>> member_id(UUID(int=1), 100) == member_id(UUID(int=1), 100)
        True
    """
    return uuid5(MEMBER_NAMESPACE, f"{pool_id}:{vmid}")


def desired_members(
    pool: dm.Pool,
    guests: Sequence[GuestInfo],
    addresses: dict[int, tuple[str, ...] | None],
) -> list[dict[str, Any]]:
    """Compute the member list a discovery pool should have.

    Args:
        pool: A pool with ``discovery`` set.
        guests: Cluster guests (any tags; they are filtered here).
        addresses: ``vmid -> addresses`` as the guests reported them; ``None`` is "cannot say".

    Returns:
        Member bodies, ordered by guest id.

    Example:
        >>> pool = dm.Pool(
        ...     id=UUID(int=1), tenant_id=UUID(int=2), name="web",
        ...     discovery=dm.PoolDiscovery(tag="sy-acme.web", cidr="10.0.0.0/24", port=80),
        ... )
        >>> guest = GuestInfo(100, "pve1", "web-1", "running", "qemu", ("sy-acme.web",))
        >>> [m["address"] for m in desired_members(pool, [guest], {100: ("10.0.0.7",)})]
        ['10.0.0.7']
    """
    discovery = pool.discovery
    if discovery is None:
        return []
    current = {m.id: m for m in pool.members}
    members: list[dict[str, Any]] = []
    for guest in sorted(guests, key=lambda g: g.vmid):
        if discovery.tag not in guest.tags or guest.status != "running":
            continue
        mid = member_id(pool.id, guest.vmid)
        known = current.get(mid)
        reported = addresses.get(guest.vmid)
        if reported is None:
            address = str(known.address) if known else None
        else:
            address = pick_address(reported, discovery.cidr)
        if address is None:
            continue
        members.append(
            {
                "id": str(mid),
                "name": f"pve-{guest.vmid}",
                "address": address,
                "port": discovery.port,
                "weight": known.weight if known else discovery.weight,
                "admin_state": (known.admin_state if known else dm.AdminState.ENABLED).value,
            }
        )
    return members


def _current(pool: dm.Pool) -> list[dict[str, Any]]:
    return [
        {
            "id": str(m.id),
            "name": m.server_name,
            "address": str(m.address),
            "port": m.port,
            "weight": m.weight,
            "admin_state": m.admin_state.value,
        }
        for m in pool.members
    ]


class DiscoveryService:
    """Runs pool discovery for every tenant."""

    def __init__(self, db: Databases, proxmox: Proxmox, change_sets: ChangeSetService) -> None:
        """Create the service.

        Args:
            db: Database handles.
            proxmox: The Proxmox client (a fake in tests).
            change_sets: Used to open and apply the change sets.
        """
        self._db = db
        self._proxmox = proxmox
        self._change_sets = change_sets

    async def run(self) -> dict[str, int]:
        """Discover members for every tenant. Returns how many pools changed per tenant name.

        A failure for one tenant is logged and does not stop the others.
        """
        async with admin_session(self._db.admin) as session:
            tenants = list((await session.execute(select(TenantRow.id, TenantRow.name))).all())
        changed: dict[str, int] = {}
        for tenant_id, tenant_name in tenants:
            try:
                count = await self.run_tenant(tenant_id)
            except (ProxmoxError, PermissionDeniedError, ValueError) as exc:
                log.error("pool discovery for %s failed: %s", tenant_name, exc)
                continue
            if count:
                changed[tenant_name] = count
        return changed

    async def run_tenant(self, tenant_id: UUID) -> int:
        """Discover members for one tenant's pools. Returns the number of pools changed."""
        async with tenant_session(self._db.app, [tenant_id]) as session:
            snapshot = await load_snapshot(session)
        pools = [p for p in snapshot.pools if p.discovery is not None]
        if not pools:
            return 0
        who = Principal(ACTOR, Role.OPERATOR, frozenset({tenant_id}))
        open_sets = await self._change_sets.list_change_sets(who, tenant_id, states=_OPEN_STATES)
        if any(c.author == ACTOR for c in open_sets):
            return 0
        guests = await self._proxmox.list_guests()
        wanted_tags = {p.discovery.tag for p in pools if p.discovery}
        relevant = [g for g in guests if wanted_tags & set(g.tags)]
        addresses: dict[int, tuple[str, ...] | None] = {}
        for guest in relevant:
            if guest.status == "running":
                addresses[guest.vmid] = await self._proxmox.guest_addresses(
                    guest.node, guest.vmid, guest.kind
                )
        ops: list[ChangeOp] = []
        names: list[str] = []
        for pool in pools:
            members = desired_members(pool, relevant, addresses)
            if members == _current(pool):
                continue
            if not members and pool.members:
                log.warning("discovery would empty pool %s; leaving it alone", pool.name)
                continue
            ops.append(
                ChangeOp(
                    object_type=ObjectType.POOL,
                    object_id=pool.id,
                    action=Action.UPDATE,
                    body={"members": members},
                    expected_version=pool.version,
                )
            )
            names.append(pool.name)
        if not ops:
            return 0
        opened = await self._change_sets.open(
            who, tenant_id, f"discovery: update members of {', '.join(names)}"
        )
        change_set_id = opened.record.id
        await self._change_sets.add_ops(who, tenant_id, change_set_id, ops)
        try:
            await self._change_sets.apply(who, tenant_id, change_set_id)
        except PermissionDeniedError:
            await self._change_sets.request_approval(who, tenant_id, change_set_id)
            log.warning("discovery change set %s waits for approval", change_set_id)
        return len(ops)
