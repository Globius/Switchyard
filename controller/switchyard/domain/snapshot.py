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
from switchyard.domain.field_rules import UDP_FORBIDDEN_PORT_FIELDS, UDP_FORBIDDEN_SERVICE_FIELDS
from switchyard.domain.models import (
    ATTACHABLE_POLICY_KINDS,
    CACHE_MAX_MB_PER_GROUP,
    HTTP_ONLY_PERSISTENCE,
    AccessPolicySet,
    Algorithm,
    AllowListPolicySet,
    BgpProfile,
    Certificate,
    ClientCertPolicySet,
    DirectoryPolicySet,
    Entity,
    ErrorPagesPolicySet,
    HealthMonitor,
    IpvsForward,
    JwtPolicySet,
    LdapConnector,
    LocalConnector,
    Model,
    MonitorType,
    Network,
    NetworkRole,
    PolicySet,
    Pool,
    PosturePolicySet,
    ProtectionPolicySet,
    Protocol,
    RadiusConnector,
    RateLimitPolicySet,
    RateLimitRule,
    RoutingProfile,
    SeGroup,
    Tenant,
    VipPool,
    VirtualService,
    WafPolicySet,
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
        routing_profiles: VRRP and BGP profiles of SE groups.
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
    routing_profiles: tuple[RoutingProfile, ...] = Field(default=())

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
            pool_ids.update(pid for r in s.routes for pid in r.pool_ids)
            cert_ids.update(s.certificate_ids)
            cert_ids.update(p.client_ca_id for p in s.ports if p.client_ca_id)
            policy_ids.update(s.policy_set_ids)
            if s.allow_list_id:
                policy_ids.add(s.allow_list_id)
            if s.deny_list_id:
                policy_ids.add(s.deny_list_id)
        for ps in self.policy_sets:
            if ps.id in policy_ids and isinstance(ps, AccessPolicySet) and ps.oidc.directory_id:
                policy_ids.add(ps.oidc.directory_id)
        pools = tuple(p for p in self.pools if p.id in pool_ids)
        cert_ids.update(p.client_certificate_id for p in pools if p.client_certificate_id)
        monitor_ids = {p.health_monitor_id for p in pools if p.health_monitor_id}
        network_ids = {n.network_id for n in group.networks}
        vip_pool_ids = _vip_pool_ids(services)
        if group.autoscale is not None:  # its deploy settings name pools that must stay valid
            vip_pool_ids |= {group.autoscale.deploy.mgmt_pool_id}
            vip_pool_ids |= set(group.autoscale.deploy.nic_pools.values())
        pool_networks = {p.network_id for p in self.vip_pools if p.id in vip_pool_ids}
        return Snapshot(
            tenants=tuple(t for t in self.tenants if t.id == group.tenant_id),
            networks=tuple(n for n in self.networks if n.id in network_ids | pool_networks),
            vip_pools=tuple(p for p in self.vip_pools if p.id in vip_pool_ids),
            se_groups=(group,),
            virtual_services=services,
            pools=pools,
            health_monitors=tuple(m for m in self.health_monitors if m.id in monitor_ids),
            certificates=tuple(c for c in self.certificates if c.id in cert_ids),
            policy_sets=tuple(p for p in self.policy_sets if p.id in policy_ids),
            routing_profiles=tuple(
                r for r in self.routing_profiles if r.id == group.routing_profile_id
            ),
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
    - ``check_port_unknown``: a monitored member without a port needs a monitor ``port``.
    - ``port_collision``: no two services bind the same VIP, port and transport.
    - ``pool_protocol_mismatch``: a pool is not shared between services of different protocols.
    - ``udp_field_not_allowed``, ``udp_algorithm_unsupported``, ``udp_member_port_required`` and
      ``monitor_protocol_mismatch``: the UDP field rules of ADR 0020, each on the offending field.
    - ``duplicate_route``: a host/path match appears at most once per service.
    - ``certificate_coverage``: every route host of a TLS-terminating HTTP service is covered by
      a SAN of the service's certificates (wildcards cover exactly one label).
    - ``profile_kind_mismatch``: an SE group's routing profile kind equals its ``ha_mode``.
    - ``wrong_policy_kind``: redirect and allow-list references point at the right kind.
    - ``discovery_tag_not_allowed``: a pool's Proxmox discovery tag starts with ``sy-<tenant>.``,
      so a tenant never discovers another tenant's guests (ADR 0145).
    - ``secret_ref_not_allowed``: certificate and BGP secret references stay in the tenant's own
      namespace and out of the platform's (ADR 0071).
    - ``wrong_certificate_purpose``: ``certificate_ids`` hold server certificates and a port's
      ``client_ca_id`` a client CA.
    - ``step_up_needs_flow`` and ``step_up_too_deep``: an access rule's ``min_factors`` needs a
      directory whose login flow has at least that many steps (ADR 0195).
    - ``pki_role_not_allowed``: a posture set's PKI role starts with its tenant's name and a dash
      (ADR 0196).
    - ``needs_health_monitor`` and ``passive_check_layer7_needs_http``: passive checks and slow
      start build on a health monitor, and ``layer7`` observation needs an HTTP-only pool.

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
    profiles = _index(snapshot.routing_profiles)

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
    _check_secret_scopes(snapshot, problems)
    _check_discovery_scopes(snapshot, problems)
    _check_directories(snapshot, problems)
    _check_posture_roles(snapshot, problems)

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
    for g in snapshot.se_groups:
        if g.peers is None:
            continue
        backend_cidrs = [
            networks[gn.network_id].cidr
            for gn in g.networks
            if gn.role is NetworkRole.BACKEND and gn.network_id in networks
        ]
        for i, peer in enumerate(g.peers.members):
            if not any(peer.address in cidr for cidr in backend_cidrs):
                problems.append(
                    Problem(
                        f"se_groups.{g.name}.peers.members[{i}].address",
                        "peer_outside_backend_network",
                        f"{peer.address} is not in a backend network of the group",
                    )
                )
    for g in snapshot.se_groups:
        _check_autoscale(g, networks, vip_pools, problems)
    for g in snapshot.se_groups:
        if g.routing_profile_id is None:
            continue
        profile = check_ref(
            f"se_groups.{g.name}.routing_profile_id",
            g.tenant_id,
            "routing profile",
            g.routing_profile_id,
            profiles,
        )
        if profile is not None and profile.kind != g.ha_mode.value:
            problems.append(
                Problem(
                    f"se_groups.{g.name}.routing_profile_id",
                    "profile_kind_mismatch",
                    f"ha_mode {g.ha_mode.value} needs a {g.ha_mode.value} routing profile, "
                    f"not {profile.kind}",
                )
            )
    for pool in snapshot.pools:
        if pool.health_monitor_id:
            monitor = check_ref(
                f"pools.{pool.name}.health_monitor_id",
                pool.tenant_id,
                "health monitor",
                pool.health_monitor_id,
                monitors,
            )
            if monitor is not None and monitor.port is None:
                for member in pool.members:
                    if member.port is None:
                        problems.append(
                            Problem(
                                f"pools.{pool.name}.members.{member.server_name}",
                                "check_port_unknown",
                                "member has no port and its health monitor has no port to check",
                            )
                        )

    for pool in snapshot.pools:
        if pool.client_certificate_id is None:
            continue
        cert = check_ref(
            f"pools.{pool.name}.client_certificate_id",
            pool.tenant_id,
            "certificate",
            pool.client_certificate_id,
            certs,
        )
        if cert is not None and cert.purpose != "server":
            problems.append(
                Problem(
                    f"pools.{pool.name}.client_certificate_id",
                    "wrong_certificate_purpose",
                    f"certificate {cert.name} is a client CA, not a certificate with a key",
                )
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
        for i, route in enumerate(s.routes):
            if route.pool_id:
                used_pools.append((f"{base}.routes[{i}].pool_id", route.pool_id))
            used_pools += [
                (f"{base}.routes[{i}].pools[{j}].pool_id", w.pool_id)
                for j, w in enumerate(route.pools)
            ]
            used_pools += [
                (f"{base}.routes[{i}].fallback_pool_ids[{j}]", pid)
                for j, pid in enumerate(route.fallback_pool_ids)
            ]
        for path, pid in used_pools:
            if check_ref(path, s.tenant_id, "pool", pid, pools_by_id) is not None:
                pool_protocols[pid].add(s.protocol)
        for i, cid in enumerate(s.certificate_ids):
            cert = check_ref(f"{base}.certificate_ids[{i}]", s.tenant_id, "certificate", cid, certs)
            if cert is not None and cert.purpose != "server":
                problems.append(
                    Problem(
                        f"{base}.certificate_ids[{i}]",
                        "wrong_certificate_purpose",
                        f"certificate {cert.name} is a client CA, not a server certificate",
                    )
                )
        for i, port in enumerate(s.ports):
            if port.client_ca_id is None:
                continue
            ca = check_ref(
                f"{base}.ports[{i}].client_ca_id",
                s.tenant_id,
                "certificate",
                port.client_ca_id,
                certs,
            )
            if ca is not None and ca.purpose != "client_ca":
                problems.append(
                    Problem(
                        f"{base}.ports[{i}].client_ca_id",
                        "wrong_certificate_purpose",
                        f"certificate {ca.name} is a server certificate, not a client CA",
                    )
                )
        error_statuses: set[int] = set()
        for i, pid in enumerate(s.policy_set_ids):
            ps = check_ref(f"{base}.policy_set_ids[{i}]", s.tenant_id, "policy set", pid, policies)
            if ps is not None and not isinstance(ps, ATTACHABLE_POLICY_KINDS):
                problems.append(
                    Problem(
                        f"{base}.policy_set_ids[{i}]",
                        "wrong_policy_kind",
                        f"policy set {ps.name} (kind {ps.kind}) cannot be attached to a service",
                    )
                )
            if isinstance(ps, ErrorPagesPolicySet):
                for page in ps.pages:
                    if page.status in error_statuses:
                        problems.append(
                            Problem(
                                f"{base}.policy_set_ids[{i}]",
                                "duplicate_error_page",
                                f"another error-page policy set already defines {page.status}",
                            )
                        )
                    error_statuses.add(page.status)
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

        if s.deny_list_id:
            ps = check_ref(
                f"{base}.deny_list_id", s.tenant_id, "policy set", s.deny_list_id, policies
            )
            if ps is not None and not isinstance(ps, AllowListPolicySet):
                problems.append(
                    Problem(
                        f"{base}.deny_list_id",
                        "wrong_policy_kind",
                        f"policy set {ps.name} is not an IP list policy set",
                    )
                )

        _check_routes(s, base, problems)
        _check_rate_limits(s, policies, base, problems)
        _check_waf(s, policies, base, problems)
        _check_protection(s, policies, base, problems)
        _check_access(s, policies, base, problems)
        _check_certificate_coverage(s, certs, base, problems)
        if s.protocol is Protocol.UDP:
            _check_udp_service(s, base, problems)
            _check_ipvs_forwarding(s, group, networks, pools_by_id, base, problems)
        _check_dns(s, base, problems)
        if s.protocol is not Protocol.UDP and s.forward_mode is not IpvsForward.NAT:
            problems.append(
                Problem(
                    f"{base}.forward_mode",
                    "forward_mode_not_allowed",
                    "forward_mode is only valid on udp services",
                )
            )

        transport = "udp" if s.protocol is Protocol.UDP else "tcp"
        listeners += [v.Listener(s.name, s.vip, p.port, transport) for p in s.ports]

    _check_udp_pools(snapshot, pool_protocols, monitors, problems)
    _check_dns_names(snapshot, problems)
    _check_failover(snapshot, problems)
    _check_cache_quota(snapshot, problems)

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
                    "pool is used by services of different protocols "
                    f"({', '.join(sorted(p.value for p in protocols))})",
                )
            )
    return sorted(problems)


def _check_discovery_scopes(snapshot: Snapshot, problems: list[Problem]) -> None:
    """Discovery tags start with the owning tenant's prefix (``discovery_tag_not_allowed``)."""
    names = {t.id: t.name for t in snapshot.tenants}
    for pool in snapshot.pools:
        if pool.discovery is None or pool.tenant_id not in names:
            continue
        prefix = v.discovery_tag_prefix(names[pool.tenant_id])
        if not pool.discovery.tag.startswith(prefix) or pool.discovery.tag == prefix:
            problems.append(
                Problem(
                    f"pools.{pool.name}.discovery.tag",
                    "discovery_tag_not_allowed",
                    f"a discovery tag must start with {prefix!r} and continue with a label",
                )
            )


def _check_directories(snapshot: Snapshot, problems: list[Problem]) -> None:
    """Access sets name a directory of their own tenant; local users have no groups."""
    directories = {p.id: p for p in snapshot.policy_sets if isinstance(p, DirectoryPolicySet)}
    for ps in snapshot.policy_sets:
        if isinstance(ps, AccessPolicySet) and ps.oidc.directory_id is None:
            if any(r.min_factors for r in ps.rules):
                problems.append(
                    Problem(
                        f"policy_sets.{ps.name}.rules",
                        "step_up_needs_flow",
                        "min_factors needs a directory with a login flow, not an OIDC issuer",
                    )
                )
            continue
        if not isinstance(ps, AccessPolicySet) or ps.oidc.directory_id is None:
            continue
        base = f"policy_sets.{ps.name}.oidc.directory_id"
        directory = directories.get(ps.oidc.directory_id)
        if directory is None:
            problems.append(
                Problem(base, "unknown_reference", "directory policy set does not exist")
            )
            continue
        if directory.tenant_id != ps.tenant_id:
            problems.append(
                Problem(base, "wrong_tenant", "directory belongs to a different tenant")
            )
        wants = max((r.min_factors or 0 for r in ps.rules), default=0)
        have = len(directory.flow.steps) if directory.flow else 0
        if wants and not have:
            problems.append(
                Problem(
                    f"policy_sets.{ps.name}.rules",
                    "step_up_needs_flow",
                    f"min_factors needs directory {directory.name} to have a login flow",
                )
            )
        elif wants > have:
            problems.append(
                Problem(
                    f"policy_sets.{ps.name}.rules",
                    "step_up_too_deep",
                    f"min_factors {wants} is more than the {have} steps of directory "
                    f"{directory.name}",
                )
            )
        if isinstance(directory.connector, LocalConnector) and any(
            r.mode == "groups" for r in ps.rules
        ):
            problems.append(
                Problem(
                    f"policy_sets.{ps.name}.rules",
                    "directory_has_no_groups",
                    f"directory {directory.name} holds local users, who have no groups",
                )
            )


def _check_posture_roles(snapshot: Snapshot, problems: list[Problem]) -> None:
    """A posture set's PKI role starts with its tenant's name and a dash (``pki_role_not_allowed``).

    Each tenant's devices are signed by a role (and so a CA) of their own; a shared role would
    make one tenant's device certificates acceptable to every other tenant's ports.
    """
    names = {t.id: t.name for t in snapshot.tenants}
    for ps in snapshot.policy_sets:
        if isinstance(ps, PosturePolicySet) and ps.tenant_id in names:
            prefix = f"{names[ps.tenant_id]}-"
            if not ps.pki_role.startswith(prefix):
                problems.append(
                    Problem(
                        f"policy_sets.{ps.name}.pki_role",
                        "pki_role_not_allowed",
                        f"the PKI role of tenant {names[ps.tenant_id]} must start with {prefix!r}",
                    )
                )


def _check_secret_scopes(snapshot: Snapshot, problems: list[Problem]) -> None:
    """Secret references stay inside the owning tenant's namespace (``secret_ref_not_allowed``)."""
    names = {t.id: t.name for t in snapshot.tenants}
    refs: list[tuple[str, UUID, str]] = []
    for c in snapshot.certificates:
        refs.append((f"certificates.{c.name}.secret_ref", c.tenant_id, c.secret_ref))
        if c.crl_secret_ref:
            refs.append((f"certificates.{c.name}.crl_secret_ref", c.tenant_id, c.crl_secret_ref))
    for ps in snapshot.policy_sets:
        if isinstance(ps, DirectoryPolicySet):
            conn = ps.connector
            where = f"policy_sets.{ps.name}.connector"
            if isinstance(conn, LdapConnector):
                refs.append((f"{where}.bind_password_ref", ps.tenant_id, conn.bind_password_ref))
            if isinstance(conn, RadiusConnector):
                refs.append((f"{where}.signing_secret_ref", ps.tenant_id, conn.signing_secret_ref))
                for i, srv in enumerate(conn.servers):
                    refs.append((f"{where}.servers[{i}].secret_ref", ps.tenant_id, srv.secret_ref))
            if isinstance(conn, LocalConnector):
                for i, user in enumerate(conn.users):
                    refs.append(
                        (
                            f"{where}.users[{i}].password_hash_ref",
                            ps.tenant_id,
                            user.password_hash_ref,
                        )
                    )
        if isinstance(ps, AccessPolicySet):
            base = f"policy_sets.{ps.name}.oidc"
            refs.append((f"{base}.client_secret_ref", ps.tenant_id, ps.oidc.client_secret_ref))
            refs.append((f"{base}.cookie_secret_ref", ps.tenant_id, ps.oidc.cookie_secret_ref))
        if isinstance(ps, ProtectionPolicySet) and ps.captcha is not None:
            refs.append(
                (f"policy_sets.{ps.name}.captcha.secret_ref", ps.tenant_id, ps.captcha.secret_ref)
            )
    for profile in snapshot.routing_profiles:
        if isinstance(profile, BgpProfile):
            for i, peer in enumerate(profile.peers):
                if peer.md5_ref:
                    path = f"routing_profiles.{profile.name}.peers[{i}].md5_ref"
                    refs.append((path, profile.tenant_id, peer.md5_ref))
    for path, tenant_id, ref in refs:
        if tenant_id in names and (why := v.check_secret_ref_scope(ref, names[tenant_id])):
            problems.append(Problem(path, "secret_ref_not_allowed", why))


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
        "routing_profiles": snapshot.routing_profiles,
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


def _check_udp_service(s: VirtualService, base: str, problems: list[Problem]) -> None:
    """Reject the fields a UDP service cannot use, each on its own path (ADR 0020)."""
    for field in UDP_FORBIDDEN_SERVICE_FIELDS:
        if getattr(s, field):
            problems.append(
                Problem(f"{base}.{field}", "udp_field_not_allowed", f"{field} is not valid on udp")
            )
    for i, port in enumerate(s.ports):
        for field in UDP_FORBIDDEN_PORT_FIELDS:
            if getattr(port, field) not in (False, None):
                problems.append(
                    Problem(
                        f"{base}.ports[{i}].{field}",
                        "udp_field_not_allowed",
                        f"{field} is not valid on udp",
                    )
                )


def _check_autoscale(
    g: SeGroup,
    networks: Mapping[UUID, Network],
    vip_pools: Mapping[UUID, VipPool],
    problems: list[Problem],
) -> None:
    """The deploy settings of an autoscaled group must fit its networks (ADR 0194)."""
    if g.autoscale is None:
        return
    base = f"se_groups.{g.name}.autoscale.deploy"
    mgmt = [gn.network_id for gn in g.networks if gn.role is NetworkRole.MGMT]
    if len(mgmt) != 1:
        problems.append(
            Problem(
                f"se_groups.{g.name}.autoscale",
                "autoscale_needs_mgmt_network",
                "an autoscaled group needs exactly one mgmt network to address new engines on",
            )
        )
        return
    pool = vip_pools.get(g.autoscale.deploy.mgmt_pool_id)
    if pool is None or pool.network_id != mgmt[0]:
        problems.append(
            Problem(
                f"{base}.mgmt_pool_id",
                "autoscale_pool_mismatch",
                "mgmt_pool_id must be a VIP pool on the group's mgmt network",
            )
        )
    data_networks = {gn.network_id for gn in g.networks if gn.role is not NetworkRole.MGMT}
    for network_id, pool_id in g.autoscale.deploy.nic_pools.items():
        pool = vip_pools.get(pool_id)
        if network_id not in data_networks or pool is None or pool.network_id != network_id:
            problems.append(
                Problem(
                    f"{base}.nic_pools.{network_id}",
                    "autoscale_pool_mismatch",
                    "each nic_pools entry must name a VIP pool on one of the group's data networks",
                )
            )
    for network_id in (*mgmt, *data_networks):
        if networks.get(network_id) is not None and not networks[network_id].vnet:
            problems.append(
                Problem(
                    f"se_groups.{g.name}.networks",
                    "autoscale_network_without_vnet",
                    f"network {networks[network_id].name} has no VNet to attach new engines to",
                )
            )


def _check_dns(s: VirtualService, base: str, problems: list[Problem]) -> None:
    """Reject ``proxied`` on UDP: Cloudflare's proxy only carries HTTP(S) and TCP ports."""
    if s.dns is not None and s.dns.proxied and s.protocol is Protocol.UDP:
        problems.append(
            Problem(
                f"{base}.dns.proxied", "dns_proxy_not_allowed", "a udp service cannot be proxied"
            )
        )


def _check_dns_names(snapshot: Snapshot, problems: list[Problem]) -> None:
    """A host name is published by one service only (ADR 0193)."""
    owner: dict[tuple[UUID, str], str] = {}
    for s in snapshot.virtual_services:
        if s.dns is None:
            continue
        for i, name in enumerate(s.dns.names):
            key = (s.tenant_id, name.lower())
            if key in owner and owner[key] != s.name:
                problems.append(
                    Problem(
                        f"virtual_services.{s.name}.dns.names[{i}]",
                        "dns_name_taken",
                        f"{name} is already published by virtual service {owner[key]}",
                    )
                )
            owner.setdefault(key, s.name)


def _check_ipvs_forwarding(
    s: VirtualService,
    group: SeGroup | None,
    networks: Mapping[UUID, Network],
    pools: Mapping[UUID, Pool],
    base: str,
    problems: list[Problem],
) -> None:
    """Check the members of a ``dr`` or ``tunnel`` UDP service (ADR 0192).

    Neither method can change the destination port, so every member must listen on every service
    port. Direct return also needs the member on a layer-2 segment of the SE: its address must be
    in a backend network of the group.
    """
    if s.forward_mode is IpvsForward.NAT or s.default_pool_id is None:
        return
    pool = pools.get(s.default_pool_id)
    if pool is None:
        return
    backend_cidrs = (
        [
            networks[gn.network_id].cidr
            for gn in group.networks
            if gn.role is NetworkRole.BACKEND and gn.network_id in networks
        ]
        if group is not None
        else []
    )
    ports = {p.port for p in s.ports}
    for member in pool.members:
        path = f"pools.{pool.name}.members.{member.server_name}"
        if member.port is not None and ports != {member.port}:
            problems.append(
                Problem(
                    f"{path}.port",
                    "ipvs_port_mismatch",
                    f"forward_mode {s.forward_mode} cannot change the port: the member must use "
                    f"the service port ({', '.join(str(p) for p in sorted(ports))}) and a service "
                    "with several ports needs a pool per port",
                )
            )
        if s.forward_mode is IpvsForward.DR and not any(
            member.address in cidr for cidr in backend_cidrs
        ):
            problems.append(
                Problem(
                    f"{path}.address",
                    "ipvs_dr_not_adjacent",
                    f"{member.address} is not in a backend network of the group; direct return "
                    "needs the member on the SE's layer-2 segment (use tunnel mode otherwise)",
                )
            )


def _check_udp_pools(
    snapshot: Snapshot,
    pool_protocols: Mapping[UUID, set[Protocol]],
    monitors: Mapping[UUID, HealthMonitor],
    problems: list[Problem],
) -> None:
    """Check pools against the protocol of the services that use them.

    ``udp`` monitors belong on UDP pools only, ``tcp``/``http`` monitors on UDP pools never, and a
    UDP pool cannot use TLS, persistence, ``random`` or members without a port.
    """
    for pool in snapshot.pools:
        protocols = pool_protocols.get(pool.id, set())
        base = f"pools.{pool.name}"
        monitor = monitors.get(pool.health_monitor_id) if pool.health_monitor_id else None
        if protocols == {Protocol.UDP}:
            if pool.tls:
                problems.append(
                    Problem(
                        f"{base}.tls", "udp_field_not_allowed", "tls is not valid on a udp pool"
                    )
                )
            if pool.persistence is not None:
                problems.append(
                    Problem(
                        f"{base}.persistence",
                        "udp_field_not_allowed",
                        "persistence is not valid on a udp pool; use the source algorithm",
                    )
                )
            for field in ("passive_check", "slow_start_s", "send_proxy", "client_certificate_id"):
                if getattr(pool, field) is not None:
                    problems.append(
                        Problem(
                            f"{base}.{field}",
                            "udp_field_not_allowed",
                            f"{field} is not valid on a udp pool",
                        )
                    )
            if pool.tunnel_timeout_s is not None:
                problems.append(
                    Problem(
                        f"{base}.tunnel_timeout_s",
                        "udp_field_not_allowed",
                        "tunnel_timeout_s is not valid on a udp pool",
                    )
                )
            if pool.backend_protocol != "http1":
                problems.append(
                    Problem(
                        f"{base}.backend_protocol",
                        "udp_field_not_allowed",
                        "backend_protocol is not valid on a udp pool",
                    )
                )
            if pool.algorithm is Algorithm.RANDOM:
                problems.append(
                    Problem(
                        f"{base}.algorithm",
                        "udp_algorithm_unsupported",
                        "IPVS has no random scheduler; use roundrobin, leastconn or source",
                    )
                )
            for member in pool.members:
                if member.port is None:
                    problems.append(
                        Problem(
                            f"{base}.members.{member.server_name}.port",
                            "udp_member_port_required",
                            "a member of a udp pool needs a port",
                        )
                    )
                for field in ("backup", "maxconn"):
                    if getattr(member, field):
                        problems.append(
                            Problem(
                                f"{base}.members.{member.server_name}.{field}",
                                "udp_field_not_allowed",
                                f"{field} is not valid on a member of a udp pool",
                            )
                        )
            if monitor is not None and monitor.type is not MonitorType.UDP:
                problems.append(
                    Problem(
                        f"{base}.health_monitor_id",
                        "monitor_protocol_mismatch",
                        f"a {monitor.type} monitor cannot check a udp pool; use a udp monitor",
                    )
                )
        else:
            _check_live_traffic(pool, protocols, monitor, base, problems)
        if protocols != {Protocol.UDP} and (
            monitor is not None and monitor.type is MonitorType.UDP and protocols
        ):
            problems.append(
                Problem(
                    f"{base}.health_monitor_id",
                    "monitor_protocol_mismatch",
                    "a udp monitor can only check a pool used by udp services",
                )
            )


def _check_live_traffic(
    pool: Pool,
    protocols: set[Protocol],
    monitor: HealthMonitor | None,
    base: str,
    problems: list[Problem],
) -> None:
    """Passive checks and slow start build on the health monitor; ``layer7`` needs HTTP.

    Codes: ``needs_health_monitor`` (on ``passive_check`` or ``slow_start_s``),
    ``passive_check_layer7_needs_http`` and ``persistence_needs_http`` (cookie, header and URI
    persistence on a pool a ``tcp`` service uses).
    """
    if pool.backend_protocol != "http1" and Protocol.TCP in protocols:
        problems.append(
            Problem(
                f"{base}.backend_protocol",
                "backend_protocol_needs_http",
                f"backend_protocol {pool.backend_protocol} needs a pool used only by http services",
            )
        )
    if isinstance(pool.persistence, HTTP_ONLY_PERSISTENCE) and Protocol.TCP in protocols:
        problems.append(
            Problem(
                f"{base}.persistence",
                "persistence_needs_http",
                f"{pool.persistence.type} persistence needs a pool used only by http services; "
                "use source_ip for tcp",
            )
        )
    for field in ("passive_check", "slow_start_s"):
        if getattr(pool, field) is not None and monitor is None:
            problems.append(
                Problem(
                    f"{base}.{field}",
                    "needs_health_monitor",
                    f"{field} needs a health monitor on the pool",
                )
            )
    if (
        pool.passive_check is not None
        and pool.passive_check.observe == "layer7"
        and Protocol.TCP in protocols
    ):
        problems.append(
            Problem(
                f"{base}.passive_check.observe",
                "passive_check_layer7_needs_http",
                "layer7 observation needs a pool used only by http services; use layer4 for tcp",
            )
        )


#: Rate-limit rules one service may carry (one stick counter each; HAProxy tracks few per stream).
MAX_RATE_LIMITS_PER_SERVICE = 8
#: Stick-table entries one service may ask for in total (memory on the SE).
MAX_TABLE_ENTRIES_PER_SERVICE = 2_000_000


def rate_limit_rules(
    s: VirtualService, policies: Mapping[UUID, Any]
) -> list[tuple[RateLimitPolicySet, int, RateLimitRule]]:
    """The rate-limit rules of a service in render order: ``(set, 1-based index in set, rule)``."""
    out = []
    for pid in s.policy_set_ids:
        ps = policies.get(pid)
        if isinstance(ps, RateLimitPolicySet):
            out += [(ps, i, r) for i, r in enumerate(ps.rules, start=1)]
    return out


def stick_table_entries(snapshot: "Snapshot", tenant_id: UUID) -> int:
    """Stick-table entries a tenant's rate-limit rules ask for, counted once per service using them.

    Example:
        >>> stick_table_entries(Snapshot(), UUID(int=1))
        0
    """
    policies = {p.id: p for p in snapshot.policy_sets}
    total = 0
    for s in snapshot.virtual_services:
        if s.tenant_id != tenant_id:
            continue
        total += sum(r.table_size for _, _, r in rate_limit_rules(s, policies))
        protection = protection_set(s, policies)
        if protection is not None and protection.tracks_connections:
            total += protection.table_size
    return total


def _check_protection(
    s: VirtualService, policies: Mapping[UUID, Any], base: str, problems: list[Problem]
) -> None:
    """A service carries at most one protection set (``too_many_protection_sets``, ADR 0170)."""
    sets = [pid for pid in s.policy_set_ids if isinstance(policies.get(pid), ProtectionPolicySet)]
    if len(sets) > 1:
        problems.append(
            Problem(
                f"{base}.policy_set_ids",
                "too_many_protection_sets",
                "a service can carry only one protection policy set",
            )
        )


def protection_set(s: VirtualService, policies: Mapping[UUID, Any]) -> ProtectionPolicySet | None:
    """The protection set of a service, if it has one."""
    for pid in s.policy_set_ids:
        ps = policies.get(pid)
        if isinstance(ps, ProtectionPolicySet):
            return ps
    return None


def _check_waf(
    s: VirtualService, policies: Mapping[UUID, Any], base: str, problems: list[Problem]
) -> None:
    """A service carries at most one WAF policy (``too_many_waf_policies``, ADR 0140)."""
    wafs = [pid for pid in s.policy_set_ids if isinstance(policies.get(pid), WafPolicySet)]
    if len(wafs) > 1:
        problems.append(
            Problem(
                f"{base}.policy_set_ids",
                "too_many_waf_policies",
                "a service can carry only one WAF policy",
            )
        )


def _check_rate_limits(
    s: VirtualService, policies: Mapping[UUID, Any], base: str, problems: list[Problem]
) -> None:
    rules = rate_limit_rules(s, policies)
    if len(rules) > MAX_RATE_LIMITS_PER_SERVICE:
        problems.append(
            Problem(
                f"{base}.policy_set_ids",
                "too_many_rate_limits",
                f"a service may have {MAX_RATE_LIMITS_PER_SERVICE} rate-limit rules; "
                f"this one has {len(rules)}",
            )
        )
    if sum(r.table_size for _, _, r in rules) > MAX_TABLE_ENTRIES_PER_SERVICE:
        problems.append(
            Problem(
                f"{base}.policy_set_ids",
                "stick_table_too_large",
                f"rate-limit tables of one service may hold {MAX_TABLE_ENTRIES_PER_SERVICE} "
                "entries in total",
            )
        )
    delays = {r.tarpit_s for _, _, r in rules if r.action == "tarpit"}
    if len(delays) > 1:
        problems.append(
            Problem(
                f"{base}.policy_set_ids",
                "tarpit_conflict",
                "tarpit rules of one service must use the same tarpit_s (one timeout per frontend)",
            )
        )


def _check_failover(snapshot: Snapshot, problems: list[Problem]) -> None:
    """A priority chain needs health checks on all pools but the last.

    Code: ``failover_needs_health_monitor``.

    Without a check HAProxy counts a down server as up, so traffic would never move on.
    """
    pools = {p.id: p for p in snapshot.pools}
    for s in snapshot.virtual_services:
        for i, route in enumerate(s.routes):
            if not route.fallback_pool_ids or route.pool_id is None:
                continue
            for pid in (route.pool_id, *route.fallback_pool_ids)[:-1]:
                pool = pools.get(pid)
                if pool is not None and pool.health_monitor_id is None:
                    problems.append(
                        Problem(
                            f"virtual_services.{s.name}.routes[{i}].fallback_pool_ids",
                            "failover_needs_health_monitor",
                            f"pool {pool.name} needs a health monitor to fail over",
                        )
                    )


def _check_cache_quota(snapshot: Snapshot, problems: list[Problem]) -> None:
    """The caches of one SE group share a memory quota (``cache_quota_exceeded``, ADR 0117)."""
    for group in snapshot.se_groups:
        total = sum(
            s.cache.size_mb
            for s in snapshot.virtual_services
            if s.se_group_id == group.id and s.enabled and s.cache is not None
        )
        if total > CACHE_MAX_MB_PER_GROUP:
            problems.append(
                Problem(
                    f"se_groups.{group.name}",
                    "cache_quota_exceeded",
                    f"caches of the group's services use {total} MB; the limit is "
                    f"{CACHE_MAX_MB_PER_GROUP} MB",
                )
            )


def _check_access(
    s: VirtualService, policies: Mapping[UUID, Any], base: str, problems: list[Problem]
) -> None:
    """A service has at most one access set and offers a login only over TLS."""
    sets = [p for pid in s.policy_set_ids if isinstance(p := policies.get(pid), AccessPolicySet)]
    attached = [policies[pid] for pid in s.policy_set_ids if pid in policies]
    tokens = [p for p in attached if isinstance(p, JwtPolicySet)]
    certs = [p for p in attached if isinstance(p, ClientCertPolicySet)]
    _check_identity_headers(sets, tokens, certs, base, problems)
    if len(certs) > 1:
        problems.append(
            Problem(
                f"{base}.policy_set_ids",
                "multiple_client_cert_sets",
                "a service may attach one client-certificate policy set",
            )
        )
    if certs and not any(p.client_ca_id for p in s.ports):
        problems.append(
            Problem(
                f"{base}.ports",
                "client_cert_needs_client_ca",
                "a client-certificate policy set needs a port with client_ca_id",
            )
        )
    if any(r.posture and r.posture.device_certificate for a in sets for r in a.rules) and not any(
        p.client_ca_id for p in s.ports
    ):
        problems.append(
            Problem(
                f"{base}.ports",
                "posture_needs_client_ca",
                "a device-certificate posture rule needs a port with client_ca_id",
            )
        )
    if len(sets) > 1:
        problems.append(
            Problem(
                f"{base}.policy_set_ids",
                "multiple_access_sets",
                "a service may attach one access policy set",
            )
        )
    if sets or tokens:
        for i, port in enumerate(s.ports):
            if not port.tls and port.https_redirect_port is None:
                problems.append(
                    Problem(
                        f"{base}.ports[{i}]",
                        "access_needs_tls",
                        f"port {port.port} would carry credentials in clear text; make it a TLS "
                        "port or redirect it to one (https_redirect_port)",
                    )
                )


def _check_identity_headers(
    access: list[AccessPolicySet],
    tokens: list[JwtPolicySet],
    certs: list[ClientCertPolicySet],
    base: str,
    problems: list[Problem],
) -> None:
    """No two policies on a service may write the same identity header."""
    names: list[str] = []
    for a in access:
        h = a.identity_headers
        names += [n.lower() for n in (h.user, h.email, h.groups, h.token) if n]
    for t in tokens:
        names += [r.subject_header.lower() for r in t.rules if r.subject_header]
    for c in certs:
        names += [n.lower() for n in (c.user_header, c.groups_header) if n]
    if len(set(names)) != len(names):
        problems.append(
            Problem(
                f"{base}.policy_set_ids",
                "identity_header_conflict",
                "two policies on this service write the same identity header",
            )
        )


def _check_routes(s: VirtualService, base: str, problems: list[Problem]) -> None:
    if s.protocol is Protocol.TCP:
        for i, r in enumerate(s.routes):
            if (
                r.match.host is None
                or r.match.path_prefix
                or r.match.path_exact
                or r.redirect
                or r.pools
            ):
                problems.append(
                    Problem(
                        f"{base}.routes[{i}]",
                        "passthrough_route_invalid",
                        "a tcp route matches the SNI host only and names a pool",
                    )
                )
    seen: set[str] = set()
    for i, r in enumerate(s.routes):
        key = r.match.model_dump_json()
        if key in seen:
            problems.append(
                Problem(
                    f"{base}.routes[{i}]",
                    "duplicate_route",
                    "same match as an earlier route",
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
