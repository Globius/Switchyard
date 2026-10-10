"""Which services a change reaches: the "Impact" tab of a change-set review.

Pure functions over two snapshots (before and after); no I/O.
"""

from dataclasses import dataclass, field
from uuid import UUID

from switchyard.domain.changes import Draft, ObjectType
from switchyard.domain.models import VirtualService
from switchyard.domain.snapshot import Snapshot


@dataclass(frozen=True)
class Impact:
    """What a draft touches, directly or through something a service references.

    Attributes:
        services: Virtual services that are created, changed, deleted or that use a changed
            object (pool, monitor, certificate, policy set, SE group, network, VIP pool, routing
            profile), as ``(id, name, vip, se_group_id)``, by name.
        vips: Their VIP addresses, ascending.
        se_group_ids: SE groups whose engines would receive new configuration.
        hostnames: Hosts of the affected services' routes, sorted.
    """

    services: tuple[tuple[UUID, str, str, UUID], ...] = field(default=())
    vips: tuple[str, ...] = field(default=())
    se_group_ids: tuple[UUID, ...] = field(default=())
    hostnames: tuple[str, ...] = field(default=())


def _changed(draft: Draft) -> dict[ObjectType, set[UUID]]:
    out: dict[ObjectType, set[UUID]] = {t: set() for t in ObjectType}
    for obj in draft.objects:
        out[obj.object_type].add(obj.object_id)
    return out


def _uses(svc: VirtualService, changed: dict[ObjectType, set[UUID]], pool_ids: set[UUID]) -> bool:
    pools = {r.pool_id for r in svc.routes if r.pool_id}
    if svc.default_pool_id:
        pools.add(svc.default_pool_id)
    return bool(
        svc.id in changed[ObjectType.VIRTUAL_SERVICE]
        or svc.se_group_id in changed[ObjectType.SE_GROUP]
        or (svc.vip_pool_id and svc.vip_pool_id in changed[ObjectType.VIP_POOL])
        or pools & (changed[ObjectType.POOL] | pool_ids)
        or set(svc.certificate_ids) & changed[ObjectType.CERTIFICATE]
        or set(svc.policy_set_ids) & changed[ObjectType.POLICY_SET]
        or (svc.allow_list_id and svc.allow_list_id in changed[ObjectType.POLICY_SET])
    )


def impact(before: Snapshot, draft: Draft) -> Impact:
    """Compute the impact of ``draft`` (proposed) against ``before`` (live).

    A deleted service is found in ``before``; every other one in the proposed state. Changing a
    health monitor, network, VIP pool or routing profile reaches the pools, SE groups and thus
    services that reference it.

    Example:
        >>> impact(snapshot, draft).hostnames  # doctest: +SKIP
        ('shop.example.test',)
    """
    after = draft.snapshot
    changed = _changed(draft)
    # Second-order references: a monitor reaches its pools; a network, VIP pool or routing
    # profile reaches the SE groups that use it.
    pool_ids = {
        p.id
        for snap in (before, after)
        for p in snap.pools
        if p.health_monitor_id and p.health_monitor_id in changed[ObjectType.HEALTH_MONITOR]
    }
    group_ids = set(changed[ObjectType.SE_GROUP])
    for snap in (before, after):
        for g in snap.se_groups:
            if {n.network_id for n in g.networks} & changed[ObjectType.NETWORK] or (
                g.routing_profile_id and g.routing_profile_id in changed[ObjectType.ROUTING_PROFILE]
            ):
                group_ids.add(g.id)
        for vp in snap.vip_pools:
            if vp.network_id in changed[ObjectType.NETWORK]:
                changed[ObjectType.VIP_POOL].add(vp.id)
    changed[ObjectType.SE_GROUP] |= group_ids
    found: dict[UUID, VirtualService] = {}
    for snap in (before, after):
        for svc in snap.virtual_services:
            if _uses(svc, changed, pool_ids):
                found[svc.id] = after_or_before(svc, after)
    ordered = sorted(found.values(), key=lambda s: (s.name, str(s.id)))
    groups = {s.se_group_id for s in ordered} | changed[ObjectType.SE_GROUP]
    known = {g.id for snap in (before, after) for g in snap.se_groups}
    hosts = {r.match.host for s in ordered for r in s.routes if r.match.host}
    return Impact(
        services=tuple((s.id, s.name, str(s.vip), s.se_group_id) for s in ordered),
        vips=tuple(sorted({str(s.vip) for s in ordered}, key=_vip_key)),
        se_group_ids=tuple(sorted(groups & known, key=str)),
        hostnames=tuple(sorted(hosts)),
    )


def after_or_before(svc: VirtualService, after: Snapshot) -> VirtualService:
    """The service as proposed when it still exists, otherwise as it was."""
    return next((s for s in after.virtual_services if s.id == svc.id), svc)


def _vip_key(text: str) -> tuple[int, int]:
    import ipaddress

    a = ipaddress.ip_address(text)
    return (a.version, int(a))
