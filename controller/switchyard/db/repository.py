# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Read and write whole snapshots.

M1 only needs two operations: store a set of new objects (used by ``syctl seed`` and tests) and load
everything the current session may see. Change sets, optimistic-lock updates and per-object CRUD
arrive with M2 in the service layer.

Loading goes through the session's tenant scope, so a tenant-scoped session returns only that
tenant's objects: RLS does the filtering, not this code.
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from switchyard.db import models as m
from switchyard.db.mapping import rows_to_snapshot, snapshot_to_rows
from switchyard.domain.snapshot import Snapshot


async def add_snapshot(session: AsyncSession, snapshot: Snapshot) -> None:
    """Insert every object of ``snapshot``.

    Rows come from ``snapshot_to_rows`` already in dependency order; they are flushed whenever the
    table changes, because the models declare no ORM relationships for SQLAlchemy to sort by. Fails
    on duplicate ids or names and on any cross-tenant reference.
    """
    previous: type | None = None
    for row in snapshot_to_rows(snapshot):
        if previous is not None and type(row) is not previous:
            await session.flush()
        session.add(row)
        previous = type(row)
    await session.flush()


async def load_snapshot(session: AsyncSession) -> Snapshot:
    """Load every object visible to the session into a :class:`Snapshot`.

    Objects are ordered by id (UUIDv7, so by creation time) to keep results stable.
    """

    async def rows[T](model: type[T], *order: object) -> list[T]:
        result = await session.execute(select(model).order_by(*order))  # type: ignore[arg-type]
        return list(result.scalars())

    return rows_to_snapshot(
        tenants=await rows(m.TenantRow, m.TenantRow.id),
        networks=await rows(m.NetworkRow, m.NetworkRow.id),
        vip_pools=await rows(m.VipPoolRow, m.VipPoolRow.id),
        se_groups=await rows(m.SeGroupRow, m.SeGroupRow.id),
        routing_profiles=await rows(m.RoutingProfileRow, m.RoutingProfileRow.id),
        se_group_networks=await rows(m.SeGroupNetworkRow, m.SeGroupNetworkRow.se_group_id),
        health_monitors=await rows(m.HealthMonitorRow, m.HealthMonitorRow.id),
        certificates=await rows(m.CertificateRow, m.CertificateRow.id),
        policy_sets=await rows(m.PolicySetRow, m.PolicySetRow.id),
        pools=await rows(m.PoolRow, m.PoolRow.id),
        pool_members=await rows(m.PoolMemberRow, m.PoolMemberRow.pool_id),
        virtual_services=await rows(m.VirtualServiceRow, m.VirtualServiceRow.id),
        vs_ports=await rows(m.VsPortRow, m.VsPortRow.vs_id),
        vs_routes=await rows(m.VsRouteRow, m.VsRouteRow.vs_id),
        vs_certificates=await rows(m.VsCertificateRow, m.VsCertificateRow.vs_id),
        vs_policy_sets=await rows(m.VsPolicySetRow, m.VsPolicySetRow.vs_id),
    )


async def find_tenant_id(session: AsyncSession, name: str) -> UUID | None:
    """Return the id of the tenant called ``name``, or ``None``."""
    result = await session.execute(select(m.TenantRow.id).where(m.TenantRow.name == name))
    return result.scalar_one_or_none()


async def find_se_group_id(session: AsyncSession, tenant_id: UUID, name: str) -> UUID | None:
    """Return the id of SE group ``name`` in ``tenant_id``, or ``None``."""
    result = await session.execute(
        select(m.SeGroupRow.id).where(
            m.SeGroupRow.tenant_id == tenant_id, m.SeGroupRow.name == name
        )
    )
    return result.scalar_one_or_none()
