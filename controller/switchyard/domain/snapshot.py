"""A point-in-time view of desired state, and the cross-object rules that validate it.

A :class:`Snapshot` holds every object the renderer needs. :func:`validate_snapshot` runs the checks
that need more than one object: references, tenancy, VIP placement, port collisions and certificate
coverage. Pydantic already checked each object on its own.

Example:
    >>> validate_snapshot(Snapshot())
    []
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from pydantic import Field

from switchyard.domain import validators as v
from switchyard.domain.errors import DomainValidationError, Problem
from switchyard.domain.models import (
    AllowListPolicySet,
    Certificate,
    Entity,
    HealthMonitor,
    Model,
    Network,
    NetworkRole,
    PolicySet,
    Pool,
    Protocol,
    RedirectPolicySet,
    SeGroup,
    Tenant,
    VipPool,
    VirtualService,
)


class Snapshot(Model):
    """All desired-state objects visible to a render or a validation run.

    Attributes:
        tenants: Tenants that own the objects below.
        networks: Networks.
        vip_pools: VIP pools.
        se_groups: SE groups.
        virtual_services: Virtual services.
        pools: Pools.
        health_monitors: Health monitors.
        certificates: Certificate metadata.
        policy_sets: Redirect and allow-list policy sets.
    """

    tenants: tuple[Tenant, ...] = ()
    networks: tuple[Network, ...] = ()
    vip_pools: tuple[VipPool, ...] = ()
    se_groups: tuple[SeGroup, ...] = ()
    virtual_services: tuple[VirtualService, ...] = ()
    pools: tuple[Pool, ...] = ()
    health_monitors: tuple[HealthMonitor, ...] = ()
    certificates: tuple[Certificate, ...] = ()
    policy_sets: tuple[PolicySet, ...] = Field(default=())

    def for_se_group(self, se_group_id: UUID) -> "Snapshot":
        """Return the subset of the snapshot a single SE group needs to render.

        Keeps the group, its virtual services and everything those reference, so renders for one
        group are independent of changes to others.

        Raises:
            KeyError: If the group does not exist.

        Example:
            >>> Snapshot().for_se_group(UUID(int=1))
            Traceback (most recent call last):
                ...
            KeyError: UUID('00000000-0000-0000-0000-000000000001')
        """
        group = next((g for g in self.se_groups if g.id == se_group_id), None)
        if group is None:
            raise KeyError(se_group_id)
        services = tuple(s for s in self.virtual_services if s.se_group_id == se_group_id)
        pool_ids: set[UUID] = set()
        cert_ids: set[UUID] = set()
        policy_ids: set[UUID] = set()
        for s in services:
            if s.default_pool_id:
                pool_ids.add(s.default_pool_id)
            pool_ids.update(r.pool_id for r in s.routes if r.pool_id)
            cert_ids.update(s.certificate_ids)
            policy_ids.update(s.policy_set_ids)
            if s.allow_list_id:
                policy_ids.add(s.allow_list_id)
        pools = tuple(p for p in self.pools if p.id in pool_ids)
        monitor_ids = {p.health_monitor_id for p in pools if p.health_monitor_id}
        network_ids = {n.network_id for n in group.networks}
        pool_networks = {p.network_id for p in self.vip_pools if p.id in _vip_pool_ids(services)}
        return Snapshot(
            tenants=tuple(t for t in self.tenants if t.id == group.tenant_id),
            networks=tuple(n for n in self.networks if n.id in network_ids | pool_networks),
            vip_pools=tuple(p for p in self.vip_pools if p.id in _vip_pool_ids(services)),
            se_groups=(group,),
            virtual_services=services,
            pools=pools,
            health_monitors=tuple(m for m in self.health_monitors if m.id in monitor_ids),
            certificates=tuple(c for c in self.certificates if c.id in cert_ids),
            policy_sets=tuple(p for p in self.policy_sets if p.id in policy_ids),
        )


def _vip_pool_ids(services: Iterable[VirtualService]) -> set[UUID]:
    return {s.vip_pool_id for s in services if s.vip_pool_id}


def _index[T: Entity](items: Iterable[T]) -> dict[UUID, T]:
    return {i.id: i for i in items}


def validate_snapshot(snapshot: Snapshot) -> list[Problem]:
    """Run every cross-object domain rule and return all findings, sorted.

    Rules (``code``):

    - ``duplicate_name``: names are unique per tenant and object kind.
    - ``unknown_reference`` / ``wrong_tenant``: references resolve and stay within one tenant.
    - ``vip_outside_network``: the VIP is a usable host address in one of the SE group's ``vip``
      networks.
    - ``vip_outside_pool``: with a ``vip_pool_id``, the VIP is inside one of the pool's ranges.
    - ``port_collision``: no two services bind the same VIP, port and transport.
    - ``pool_protocol_mismatch``: a pool is not shared between HTTP and TCP services.
    - ``duplicate_route``: a host/path match appears at most once per service.
    - ``certificate_coverage``: every route host of a TLS-terminating HTTP service is covered by
      a SAN of the service's certificates (wildcards cover exactly one label).
    - ``wrong_policy_kind``: redirect and allow-list references point at the right kind.

    Returns:
        Findings sorted by path; empty when the snapshot is valid.

    Example:
        >>> validate_snapshot(Snapshot())
        []
    """
    problems: list[Problem] = []
    tenants = _index(snapshot.tenants)
    networks = _index(snapshot.networks)
    pools_by_id = _index(snapshot.pools)
    monitors = _index(snapshot.health_monitors)
    certs = _index(snapshot.certificates)
    policies: Mapping[UUID, Any] = {p.id: p for p in snapshot.policy_sets}
    vip_pools = _index(snapshot.vip_pools)
    groups = _index(snapshot.se_groups)

    def check_ref(
        path: str, tenant_id: UUID, kind: str, ref: UUID, index: Mapping[UUID, Any]
    ) -> Any:
        target = index.get(ref)
        if target is None:
            problems.append(Problem(path, "unknown_reference", f"{kind} {ref} does not exist"))
            return None
        if getattr(target, "tenant_id", tenant_id) != tenant_id:
            problems.append(
                Problem(path, "wrong_tenant", f"{kind} {ref} belongs to a different tenant")
            )
        return target

    _check_names(snapshot, problems)

    for kind, items in _tenant_owned(snapshot).items():
        for item in items:
            check_ref(
                f"{kind}.{item.name}.tenant_id", item.tenant_id, "tenant", item.tenant_id, tenants
            )
    for vp in snapshot.vip_pools:
        net = check_ref(
            f"vip_pools.{vp.name}.network_id", vp.tenant_id, "network", vp.network_id, networks
        )
        if net is not None:
            for r in vp.ranges:
                if r.start not in net.cidr or r.end not in net.cidr:
                    problems.append(
                        Problem(
                            f"vip_pools.{vp.name}.ranges",
                            "vip_outside_network",
                            f"range {r.start}-{r.end} is outside {net.cidr}",
                        )
                    )
    for g in snapshot.se_groups:
        for i, gn in enumerate(g.networks):
            check_ref(
                f"se_groups.{g.name}.networks[{i}]", g.tenant_id, "network", gn.network_id, networks
            )
    for pool in snapshot.pools:
        if pool.health_monitor_id:
            check_ref(
                f"pools.{pool.name}.health_monitor_id",
                pool.tenant_id,
                "health monitor",
                pool.health_monitor_id,
                monitors,
            )

    pool_protocols: dict[UUID, set[Protocol]] = defaultdict(set)
    listeners: list[v.Listener] = []
    for s in snapshot.virtual_services:
        base = f"virtual_services.{s.name}"
        group = check_ref(f"{base}.se_group_id", s.tenant_id, "SE group", s.se_group_id, groups)
        if s.vip_pool_id:
            vp = check_ref(f"{base}.vip_pool_id", s.tenant_id, "VIP pool", s.vip_pool_id, vip_pools)
            if vp is not None and not any(s.vip in r for r in vp.ranges):
                problems.append(
                    Problem(f"{base}.vip", "vip_outside_pool", f"{s.vip} is not in pool {vp.name}")
                )
        if group is not None:
            _check_vip(s, group, networks, base, problems)

        used_pools: list[tuple[str, UUID]] = []
        if s.default_pool_id:
            used_pools.append((f"{base}.default_pool_id", s.default_pool_id))
        used_pools += [
            (f"{base}.routes[{i}].pool_id", r.pool_id) for i, r in enumerate(s.routes) if r.pool_id
        ]
        for path, pid in used_pools:
            if check_ref(path, s.tenant_id, "pool", pid, pools_by_id) is not None:
                pool_protocols[pid].add(s.protocol)
        for i, cid in enumerate(s.certificate_ids):
            check_ref(f"{base}.certificate_ids[{i}]", s.tenant_id, "certificate", cid, certs)
        for i, pid in enumerate(s.policy_set_ids):
            ps = check_ref(f"{base}.policy_set_ids[{i}]", s.tenant_id, "policy set", pid, policies)
            if ps is not None and not isinstance(ps, RedirectPolicySet):
                problems.append(
                    Problem(
                        f"{base}.policy_set_ids[{i}]",
                        "wrong_policy_kind",
                        f"policy set {ps.name} is not a redirect policy set",
                    )
                )
        if s.allow_list_id:
            ps = check_ref(
                f"{base}.allow_list_id", s.tenant_id, "policy set", s.allow_list_id, policies
            )
            if ps is not None and not isinstance(ps, AllowListPolicySet):
                problems.append(
                    Problem(
                        f"{base}.allow_list_id",
                        "wrong_policy_kind",
                        f"policy set {ps.name} is not an allow-list policy set",
                    )
                )

        _check_routes(s, base, problems)
        _check_certificate_coverage(s, certs, base, problems)

        transport = "udp" if s.protocol is Protocol.UDP else "tcp"
        listeners += [v.Listener(s.name, s.vip, p.port, transport) for p in s.ports]

    for first, second in v.find_port_collisions(listeners):
        problems.append(
            Problem(
                f"virtual_services.{second.owner}.ports",
                "port_collision",
                f"{second.vip}:{second.port} is already bound by {first.owner}",
            )
        )
    for pid, protocols in pool_protocols.items():
        if len(protocols) > 1:
            name = pools_by_id[pid].name
            problems.append(
                Problem(
                    f"pools.{name}",
                    "pool_protocol_mismatch",
                    "pool is used by both http and tcp/udp services",
                )
            )
    return sorted(problems)


def _tenant_owned(snapshot: Snapshot) -> dict[str, Iterable[Any]]:
    return {
        "networks": snapshot.networks,
        "vip_pools": snapshot.vip_pools,
        "se_groups": snapshot.se_groups,
        "virtual_services": snapshot.virtual_services,
        "pools": snapshot.pools,
        "health_monitors": snapshot.health_monitors,
        "certificates": snapshot.certificates,
        "policy_sets": snapshot.policy_sets,
    }


def _check_names(snapshot: Snapshot, problems: list[Problem]) -> None:
    for kind, items in _tenant_owned(snapshot).items():
        seen: set[tuple[UUID, str]] = set()
        for item in items:
            key = (item.tenant_id, item.name)
            if key in seen:
                problems.append(
                    Problem(f"{kind}.{item.name}", "duplicate_name", f"duplicate {kind} name")
                )
            seen.add(key)
    seen_tenants: set[str] = set()
    for t in snapshot.tenants:
        if t.name in seen_tenants:
            problems.append(Problem(f"tenants.{t.name}", "duplicate_name", "duplicate tenant name"))
        seen_tenants.add(t.name)


def _check_vip(
    s: VirtualService,
    group: SeGroup,
    networks: Mapping[UUID, Network],
    base: str,
    problems: list[Problem],
) -> None:
    vip_nets = [
        networks[gn.network_id]
        for gn in group.networks
        if gn.role is NetworkRole.VIP and gn.network_id in networks
    ]
    reasons: list[str] = []
    for net in vip_nets:
        try:
            v.validate_vip_in_network(s.vip, net.cidr)
        except ValueError as exc:
            reasons.append(str(exc))
        else:
            return
    detail = "; ".join(reasons) if reasons else "the SE group has no vip network"
    problems.append(
        Problem(
            f"{base}.vip",
            "vip_outside_network",
            f"{s.vip} is not usable on SE group {group.name}: {detail}",
        )
    )


def _check_routes(s: VirtualService, base: str, problems: list[Problem]) -> None:
    seen: set[tuple[str | None, str | None, str | None]] = set()
    for i, r in enumerate(s.routes):
        key = (r.match.host, r.match.path_prefix, r.match.path_exact)
        if key in seen:
            problems.append(
                Problem(
                    f"{base}.routes[{i}]",
                    "duplicate_route",
                    "same host and path match as an earlier route",
                )
            )
        seen.add(key)


def _check_certificate_coverage(
    s: VirtualService, certs: Mapping[UUID, Certificate], base: str, problems: list[Problem]
) -> None:
    if s.protocol is not Protocol.HTTP or not any(p.tls for p in s.ports):
        return
    sans = [san for cid in s.certificate_ids if cid in certs for san in certs[cid].sans]
    hosts = {r.match.host for r in s.routes if r.match.host}
    for host in v.uncovered_hosts(hosts, sans):
        problems.append(
            Problem(
                f"{base}.certificate_ids",
                "certificate_coverage",
                f"no certificate SAN covers route host {host}",
            )
        )


def ensure_valid(snapshot: Snapshot) -> None:
    """Raise :class:`DomainValidationError` if the snapshot has any problem.

    Example:
        >>> ensure_valid(Snapshot())
    """
    problems = validate_snapshot(snapshot)
    if problems:
        raise DomainValidationError(problems)
