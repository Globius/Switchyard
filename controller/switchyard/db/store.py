# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Write the difference between two snapshots to the database, with optimistic locking.

:func:`apply_delta` is how an applied change set reaches the tables: creates are inserted, changed
objects are updated only if their ``version`` is still the one the change set was based on, and
deleted objects are removed. Child rows (ports, routes, members, ...) of a changed object are
replaced as a whole.

Order matters because of foreign keys: inserts first (parents before children), then updates (an
update may point at a row inserted a moment ago), then deletes in reverse dependency order.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from switchyard.db import models as m
from switchyard.db.mapping import snapshot_to_rows
from switchyard.domain.changes import COLLECTION, ObjectType
from switchyard.domain.models import Entity
from switchyard.domain.snapshot import Snapshot

#: Parent table of each object type.
PARENT: dict[ObjectType, type[DeclarativeBase]] = {
    ObjectType.NETWORK: m.NetworkRow,
    ObjectType.VIP_POOL: m.VipPoolRow,
    ObjectType.SE_GROUP: m.SeGroupRow,
    ObjectType.VIRTUAL_SERVICE: m.VirtualServiceRow,
    ObjectType.POOL: m.PoolRow,
    ObjectType.HEALTH_MONITOR: m.HealthMonitorRow,
    ObjectType.CERTIFICATE: m.CertificateRow,
    ObjectType.POLICY_SET: m.PolicySetRow,
    ObjectType.ROUTING_PROFILE: m.RoutingProfileRow,
}

#: Child tables of each object type and the column that points at the parent.
CHILDREN: dict[ObjectType, tuple[tuple[type[DeclarativeBase], str], ...]] = {
    ObjectType.SE_GROUP: ((m.SeGroupNetworkRow, "se_group_id"),),
    ObjectType.POOL: ((m.PoolMemberRow, "pool_id"),),
    ObjectType.VIRTUAL_SERVICE: (
        (m.VsPortRow, "vs_id"),
        (m.VsRouteRow, "vs_id"),
        (m.VsCertificateRow, "vs_id"),
        (m.VsPolicySetRow, "vs_id"),
    ),
}

#: Deletion order: whatever references others goes first.
_DELETE_ORDER = (
    ObjectType.VIRTUAL_SERVICE,
    ObjectType.POOL,
    ObjectType.POLICY_SET,
    ObjectType.CERTIFICATE,
    ObjectType.HEALTH_MONITOR,
    ObjectType.VIP_POOL,
    ObjectType.SE_GROUP,
    ObjectType.ROUTING_PROFILE,
    ObjectType.NETWORK,
)


class ConcurrentModificationError(RuntimeError):
    """An object changed in the database since the change set was based on it."""

    def __init__(self, object_type: ObjectType, object_id: UUID, expected: int) -> None:
        """Record which object moved."""
        self.object_type, self.object_id, self.expected = object_type, object_id, expected
        super().__init__(f"{object_type} {object_id} is no longer at version {expected}")


@dataclass(frozen=True)
class Delta:
    """Objects that differ between two snapshots, by type."""

    created: dict[ObjectType, list[UUID]]
    modified: dict[ObjectType, list[UUID]]
    deleted: dict[ObjectType, list[UUID]]

    @property
    def is_empty(self) -> bool:
        """True when nothing differs."""
        return not any(
            ids for group in (self.created, self.modified, self.deleted) for ids in group.values()
        )


def compute_delta(base: Snapshot, after: Snapshot) -> Delta:
    """Compare two snapshots by id; an object counts as modified when its version differs."""
    created: dict[ObjectType, list[UUID]] = {}
    modified: dict[ObjectType, list[UUID]] = {}
    deleted: dict[ObjectType, list[UUID]] = {}
    for object_type, name in COLLECTION.items():
        before: dict[UUID, Entity] = {o.id: o for o in getattr(base, name)}
        now: dict[UUID, Entity] = {o.id: o for o in getattr(after, name)}
        created[object_type] = sorted(now.keys() - before.keys())
        deleted[object_type] = sorted(before.keys() - now.keys())
        modified[object_type] = sorted(
            i for i in now.keys() & before.keys() if now[i].version != before[i].version
        )
    return Delta(created, modified, deleted)


def _subset(snapshot: Snapshot, ids: Mapping[ObjectType, Iterable[UUID]]) -> Snapshot:
    wanted = {t: set(v) for t, v in ids.items()}
    fields: dict[str, Any] = {}
    for object_type, name in COLLECTION.items():
        fields[name] = tuple(
            o for o in getattr(snapshot, name) if o.id in wanted.get(object_type, ())
        )
    return Snapshot.model_validate(fields)


def _column_values(row: DeclarativeBase) -> dict[str, Any]:
    return {
        c.name: getattr(row, c.key)
        for c in row.__table__.columns
        if c.name not in {"id", "tenant_id"}
    }


async def apply_delta(session: AsyncSession, base: Snapshot, after: Snapshot) -> Delta:
    """Make the database match ``after``, given that it currently matches ``base``.

    Args:
        session: A tenant-scoped (or admin) session inside a transaction.
        base: State the change set was computed against.
        after: Proposed state (versions of changed objects already raised).

    Returns:
        The delta that was written.

    Raises:
        ConcurrentModificationError: If a changed or deleted object moved past its base version.
    """
    delta = compute_delta(base, after)

    inserts = snapshot_to_rows(_subset(after, delta.created))
    previous: type | None = None
    for row in inserts:
        if previous is not None and type(row) is not previous:
            await session.flush()
        session.add(row)
        previous = type(row)
    await session.flush()

    base_versions = {
        (t, o.id): o.version for t, name in COLLECTION.items() for o in getattr(base, name)
    }
    for object_type, ids in delta.modified.items():
        for object_id in ids:
            one = _subset(after, {object_type: [object_id]})
            rows = snapshot_to_rows(one)
            parent, children = rows[0], rows[1:]
            table = PARENT[object_type]
            result = await session.execute(
                update(table)
                .where(
                    table.id == object_id,  # type: ignore[attr-defined]
                    table.version == base_versions[(object_type, object_id)],  # type: ignore[attr-defined]
                )
                .values(**_column_values(parent))
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise ConcurrentModificationError(
                    object_type, object_id, base_versions[(object_type, object_id)]
                )
            for child_table, fk in CHILDREN.get(object_type, ()):
                await session.execute(
                    delete(child_table).where(getattr(child_table, fk) == object_id)
                )
            for child in children:
                session.add(child)
            await session.flush()

    for object_type in _DELETE_ORDER:
        for object_id in delta.deleted[object_type]:
            table = PARENT[object_type]
            result = await session.execute(
                delete(table).where(
                    table.id == object_id,  # type: ignore[attr-defined]
                    table.version == base_versions[(object_type, object_id)],  # type: ignore[attr-defined]
                )
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise ConcurrentModificationError(
                    object_type, object_id, base_versions[(object_type, object_id)]
                )
    return delta
