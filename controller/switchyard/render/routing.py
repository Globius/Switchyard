# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Render the routing side of an SE group: ``keepalived.conf`` (VRRP) or ``frr.conf`` (BGP).

Like the HAProxy renderer these are pure functions of a snapshot (plus, for BGP session passwords,
the secrets the caller resolved from OpenBao). Output differs per engine only in its name, its VRRP
priority and its BGP router id, so every engine of a group is rendered separately.

Files (relative to ``/etc/switchyard/``, delivered to the agent as a signed bundle):

======================================= ===========================================================
``keepalived/keepalived.conf``          VRRP instance, health tracking, VIPs, static routes, and the
                                        IPVS ``virtual_server`` blocks of UDP services (BGP groups:
                                        IPVS only, and only when the group has UDP services)
``frr/frr.conf``                        BGP neighbours, BFD, VIP announcements, static routes
``frr/vips.json``                       ASN and VIP prefixes for the agent's route-health watchdog
                                        (``udp_prefixes``: the ones served by IPVS;
                                        ``udp_only_prefixes``: those no HAProxy service uses)
``nftables/switchyard-udp.nft``         Masquerade for traffic IPVS sends to real servers (UDP only)
``sysctl/switchyard-udp.conf``          ``ip_forward`` and ``vs.conntrack`` for IPVS NAT (UDP only)
======================================= ===========================================================

BGP announces only the group's VIPs (an out route-map) and accepts nothing from the peers.
"""

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from jinja2 import Environment, PackageLoader, StrictUndefined

from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot, ensure_valid
from switchyard.render import escape as esc
from switchyard.render.escape import RenderError

_ENV = Environment(  # nosemgrep: python-jinja2-xss-autoescape-false-atomic
    loader=PackageLoader("switchyard.render", "templates"),
    undefined=StrictUndefined,
    autoescape=False,  # noqa: S701
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)
_PASSWORD = re.compile(r"[A-Za-z0-9._~@%+=:-]{1,80}")


@dataclass(frozen=True)
class EngineSlot:
    """Which engine of the group a file set is for.

    Attributes:
        name: Engine name (letters, digits, ``-`` and ``_``).
        index: Position in the group's rollout order (0 is the canary and the VRRP master).
        router_id: BGP router id: the engine's IPv4 management address, or, for an IPv6-only
            engine, any other string (the id is then derived from the engine name).
    """

    name: str
    index: int
    router_id: str


def _vips(
    snapshot: Snapshot, group: dm.SeGroup
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    found = {s.vip for s in snapshot.virtual_services if s.se_group_id == group.id and s.enabled}
    return sorted(found, key=lambda a: (a.version, int(a)))


def _vip_prefixlen(
    snapshot: Snapshot, group: dm.SeGroup, vip: ipaddress.IPv4Address | ipaddress.IPv6Address
) -> int:
    nets = {n.id: n.cidr for n in snapshot.networks}
    for attached in group.networks:
        cidr = nets.get(attached.network_id)
        if attached.role is dm.NetworkRole.VIP and cidr is not None and vip in cidr:
            return cidr.prefixlen
    raise RenderError(f"VIP {vip} is not inside a vip network of SE group {group.name}")


#: IPVS schedulers by pool algorithm: (equal weights, unequal weights). ``random`` has no
#: equivalent and is rejected by the snapshot validator (ADR 0020).
_SCHEDULER = {
    dm.Algorithm.ROUNDROBIN: ("rr", "wrr"),
    dm.Algorithm.LEASTCONN: ("lc", "wlc"),
    dm.Algorithm.SOURCE: ("sh", "sh"),
}


#: Keepalived ``lb_kind`` by forwarding method (ADR 0192).
_LB_KIND = {
    dm.IpvsForward.NAT: "NAT",
    dm.IpvsForward.DR: "DR",
    dm.IpvsForward.TUNNEL: "TUN",
}


@dataclass(frozen=True)
class IpvsServer:
    """One IPVS virtual server: a UDP service port with its real servers.

    ``members`` holds ``(address, port, weight)``; a member without a port of its own is checked
    against validation, so every member here has one.
    """

    service: str
    vip: ipaddress.IPv4Address | ipaddress.IPv6Address
    port: int
    scheduler: str
    monitor: dm.HealthMonitor | None
    members: tuple[tuple[str, int, int], ...]
    forward: dm.IpvsForward = dm.IpvsForward.NAT


def _ipvs_servers(snapshot: Snapshot, group: dm.SeGroup) -> list[IpvsServer]:
    """The IPVS virtual servers of a group's enabled UDP services, sorted by VIP and port."""
    pools = {p.id: p for p in snapshot.pools}
    monitors = {m.id: m for m in snapshot.health_monitors}
    out = []
    for svc in snapshot.virtual_services:
        if svc.se_group_id != group.id or not svc.enabled or svc.protocol is not dm.Protocol.UDP:
            continue
        if svc.default_pool_id is None:
            raise RenderError(f"virtual service {svc.name} has no pool")
        pool = pools[svc.default_pool_id]
        monitor = monitors.get(pool.health_monitor_id) if pool.health_monitor_id else None
        try:
            plain, weighted = _SCHEDULER[pool.algorithm]
        except KeyError:
            raise RenderError(f"pool {pool.name}: IPVS has no {pool.algorithm} scheduler") from None
        for member in pool.members:
            if member.address.version != svc.vip.version:
                raise RenderError(
                    f"virtual service {svc.name}: member {member.address} is not in the address "
                    f"family of VIP {svc.vip} (IPVS cannot translate between them)"
                )
            if member.port is None:
                raise RenderError(f"pool {pool.name}: member {member.address} has no port")
        # disabled members are omitted; draining ones stay with weight 0
        members = tuple(
            sorted(
                (
                    str(m.address),
                    m.port or 0,
                    0 if m.admin_state is dm.AdminState.DRAIN else m.weight,
                )
                for m in pool.members
                if m.admin_state is not dm.AdminState.DISABLED
            )
        )
        scheduler = weighted if len({w for _, _, w in members}) > 1 else plain
        out += [
            IpvsServer(svc.name, svc.vip, port, scheduler, monitor, members, svc.forward_mode)
            for port in sorted(p.port for p in svc.ports)
        ]
    return sorted(out, key=lambda s: (s.vip.version, int(s.vip), s.port))


def _virtual_servers(servers: list[IpvsServer]) -> list[dict[str, object]]:
    """Template context for the ``virtual_server`` blocks."""
    rendered = []
    for s in servers:
        monitor = s.monitor
        udp = monitor.udp if monitor is not None else None
        rendered.append(
            {
                "service": esc.ident(s.service),
                "vip": str(s.vip),
                "port": s.port,
                "scheduler": s.scheduler,
                "lb_kind": _LB_KIND[s.forward],
                "delay_loop": monitor.interval_s if monitor else 5,
                "checked": monitor is not None,
                "retry": monitor.fall if monitor else 3,
                "timeout": monitor.timeout_s if monitor else 3,
                "check_port": monitor.port if monitor else None,
                "payload": udp.payload_hex if udp else None,
                "require_reply": bool(udp and udp.require_reply),
                "members": [{"address": a, "port": p, "weight": w} for a, p, w in s.members],
            }
        )
    return rendered


def _udp_host_files(servers: list[IpvsServer]) -> dict[str, str]:
    """The sysctls and the masquerade rule IPVS NAT needs on the SE.

    Only ``nat`` services need them: direct return and tunnel replies bypass the SE, so a group
    with only those renders no host files.
    """
    servers = [s for s in servers if s.forward is dm.IpvsForward.NAT]
    if not servers:
        return {}
    targets: dict[int, set[tuple[int, int]]] = {4: set(), 6: set()}
    for s in servers:
        for address, port, _ in s.members:
            ip = ipaddress.ip_address(address)
            targets[ip.version].add((int(ip), port))
    elements = {
        version: [
            f"{ipaddress.IPv4Address(n) if version == 4 else ipaddress.IPv6Address(n)} . {port}"
            for n, port in sorted(found)
        ]
        for version, found in targets.items()
    }
    return {
        "nftables/switchyard-udp.nft": _ENV.get_template("switchyard-udp.nft.j2").render(
            v4=elements[4], v6=elements[6]
        ),
        "sysctl/switchyard-udp.conf": _ENV.get_template("switchyard-udp.sysctl.j2").render(),
    }


def _peer_view(peer: dm.BgpPeer, profile: dm.BgpProfile) -> dict[str, object]:
    """A neighbour for the address-family sections: its address and its effective prefix limit.

    Example:
        >>> peer = dm.BgpPeer(peer_ip="10.0.0.1", remote_asn=65001, max_prefixes=10)
        >>> _peer_view(peer, dm.BgpProfile(name="p", tenant_id=UUID(int=1), id=UUID(int=2),
        ...     asn=65000, peers=(peer,), max_prefixes=100))
        {'ip': '10.0.0.1', 'max_prefixes': 10}
    """
    limit = peer.max_prefixes if peer.max_prefixes is not None else profile.max_prefixes
    return {"ip": str(peer.peer_ip), "max_prefixes": limit}


def _route_lines(routes: tuple[dm.StaticRoute, ...], style: str) -> list[str]:
    out = []
    for r in sorted(
        routes, key=lambda r: (r.prefix.version, int(r.prefix.network_address), r.prefix.prefixlen)
    ):
        if style == "keepalived":
            out.append(f"{r.prefix} via {r.next_hop}")
        else:
            keyword = "ip route" if r.prefix.version == 4 else "ipv6 route"
            out.append(f"{keyword} {r.prefix} {r.next_hop}")
    return out


def _group_and_profile(
    snapshot: Snapshot, se_group_id: UUID
) -> tuple[Snapshot, dm.SeGroup, dm.RoutingProfile]:
    try:
        view = snapshot.for_se_group(se_group_id)
    except KeyError:
        raise RenderError(f"unknown SE group {se_group_id}") from None
    ensure_valid(view)
    group = view.se_groups[0]
    if not view.routing_profiles:
        raise RenderError(f"SE group {group.name} has no routing profile")
    return view, group, view.routing_profiles[0]


def render_keepalived(snapshot: Snapshot, se_group_id: UUID, slot: EngineSlot) -> dict[str, str]:
    """Render ``keepalived/keepalived.conf`` for one engine of a VRRP group.

    The engine at ``slot.index`` 0 gets ``base_priority`` and starts as the preferred master; each
    later engine gets ``priority_step`` less. Failing the local HAProxy health check lowers the
    priority by 60, which makes another engine take over.

    Raises:
        RenderError: If the group has no VRRP profile or no virtual service.
    """
    view, group, profile = _group_and_profile(snapshot, se_group_id)
    if not isinstance(profile, dm.VrrpProfile):
        raise RenderError(f"SE group {group.name} does not use VRRP")
    all_vips = _vips(view, group)
    if not all_vips:
        raise RenderError(f"SE group {group.name} has no virtual service to announce")
    servers = _ipvs_servers(view, group)
    interface = esc.ident(profile.interface.lower())
    # VRRP carries one address family per instance (RFC 5798), so a dual-stack group gets an
    # IPv4 and an IPv6 (VRRPv3) instance that share the VRID and the priority. The IPv4 instance
    # keeps the historic name so single-stack groups render exactly as before.
    base = "sy_" + esc.ident(group.name)
    instances = []
    for family, name in ((4, base), (6, base + "_6")):
        vips = [f"{v}/{_vip_prefixlen(view, group, v)}" for v in all_vips if v.version == family]
        if not vips:
            continue
        routes = tuple(r for r in profile.static_routes if r.prefix.version == family)
        instances.append(
            {
                "name": name,
                "family": family,
                "vips": vips,
                "routes": [
                    f"{line} dev {esc.ident(profile.interface)}"
                    for line in _route_lines(routes, "keepalived")
                ],
            }
        )
    text = _ENV.get_template("keepalived.conf.j2").render(
        virtual_servers=_virtual_servers(servers),
        group=esc.ident(group.name),
        engine=esc.ident(slot.name),
        instances=instances,
        interface=interface,
        vrid=profile.vrid,
        priority=max(1, profile.base_priority - profile.priority_step * slot.index),
        advert=profile.advert_interval_s,
    )
    return {"keepalived/keepalived.conf": text} | _udp_host_files(servers)


def _render_ipvs_only(
    group: dm.SeGroup, slot: EngineSlot, servers: list[IpvsServer]
) -> dict[str, str]:
    """Keepalived for a BGP group: IPVS virtual servers and no ``vrrp_instance`` (FRR owns VIPs)."""
    text = _ENV.get_template("keepalived.conf.j2").render(
        virtual_servers=_virtual_servers(servers),
        group=esc.ident(group.name),
        engine=esc.ident(slot.name),
        instances=[],
    )
    return {"keepalived/keepalived.conf": text} | _udp_host_files(servers)


def _host_prefix(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """The host route for a VIP: ``/32`` for IPv4, ``/128`` for IPv6."""
    return f"{address}/{address.max_prefixlen}"


def bgp_router_id(slot: EngineSlot) -> str:
    """The BGP router id (a 32-bit identifier) for an engine.

    An IPv4 management address is used as is. An IPv6-only engine has none, so the id is derived
    from the engine name inside ``100.64.0.0/10`` (a range that is never a real router address
    here); it is stable across renders and only needs to be unique among the group's peers.

    Example:
        >>> bgp_router_id(EngineSlot("se-1", 0, "10.0.0.5"))
        '10.0.0.5'
        >>> bgp_router_id(EngineSlot("se-1", 0, "2001:db8::5")).startswith("100.")
        True
    """
    try:
        return str(ipaddress.IPv4Address(slot.router_id))
    except ValueError:
        pass
    digest = hashlib.sha256(slot.name.encode()).digest()
    return str(
        ipaddress.IPv4Address(
            (100 << 24) | (64 << 16) | int.from_bytes(digest[:3], "big") % (1 << 22)
        )
    )


def render_frr(
    snapshot: Snapshot, se_group_id: UUID, slot: EngineSlot, secrets: Mapping[str, str]
) -> dict[str, str]:
    """Render ``frr/frr.conf`` and ``frr/vips.json`` for one engine of a BGP group.

    Args:
        snapshot: Desired state.
        se_group_id: The group.
        slot: The engine.
        secrets: BGP session passwords by ``md5_ref``; a missing reference is an error, because
            silently dropping authentication would be worse than failing the render.
    """
    view, group, profile = _group_and_profile(snapshot, se_group_id)
    if not isinstance(profile, dm.BgpProfile):
        raise RenderError(f"SE group {group.name} does not use BGP")
    router_id = bgp_router_id(slot)
    vips = _vips(view, group)
    if not vips:
        raise RenderError(f"SE group {group.name} has no virtual service to announce")
    ordered = sorted(profile.peers, key=lambda p: (p.peer_ip.version, int(p.peer_ip)))
    peers = []
    for peer in ordered:
        password = None
        if peer.md5_ref is not None:
            if peer.md5_ref not in secrets:
                raise RenderError(f"missing secret {peer.md5_ref} for BGP peer {peer.peer_ip}")
            password = secrets[peer.md5_ref]
            if not _PASSWORD.fullmatch(password):
                raise RenderError(f"BGP password for {peer.peer_ip} contains unsafe characters")
        peers.append({"ip": str(peer.peer_ip), "asn": peer.remote_asn, "password": password})
    v4 = [_host_prefix(v) for v in vips if v.version == 4]
    v6 = [_host_prefix(v) for v in vips if v.version == 6]
    text = _ENV.get_template("frr.conf.j2").render(
        group=esc.ident(group.name),
        engine=esc.ident(slot.name),
        asn=profile.asn,
        router_id=router_id,
        peers=peers,
        bfd=profile.bfd,
        v4_vips=v4,
        v6_vips=v6,
        v4_peers=[_peer_view(p, profile) for p in ordered if p.peer_ip.version == 4],
        v6_peers=[_peer_view(p, profile) for p in ordered if p.peer_ip.version == 6],
        max_paths=profile.max_paths,
        routes=_route_lines(profile.static_routes, "frr"),
    )
    servers = _ipvs_servers(view, group)
    udp_prefixes = sorted({_host_prefix(s.vip) for s in servers})
    vips_json: dict[str, object] = {"asn": profile.asn, "prefixes": v4 + v6}
    if udp_prefixes:
        shared = {
            _host_prefix(svc.vip)
            for svc in view.virtual_services
            if svc.se_group_id == group.id and svc.enabled and svc.protocol is not dm.Protocol.UDP
        }
        vips_json["udp_prefixes"] = udp_prefixes
        vips_json["udp_only_prefixes"] = [p for p in udp_prefixes if p not in shared]
    watchdog = json.dumps(vips_json, sort_keys=True, indent=2) + "\n"
    files = {"frr/frr.conf": text, "frr/vips.json": watchdog}
    if servers:
        files |= _render_ipvs_only(group, slot, servers)
    return files


def render_agent_bundle(
    snapshot: Snapshot, se_group_id: UUID, slot: EngineSlot, secrets: Mapping[str, str]
) -> dict[str, str]:
    """Render whatever the agent needs for this engine, chosen by the group's HA mode."""
    try:
        group = next(g for g in snapshot.se_groups if g.id == se_group_id)
    except StopIteration:
        raise RenderError(f"unknown SE group {se_group_id}") from None
    if group.ha_mode is dm.HaMode.BGP:
        return render_frr(snapshot, se_group_id, slot, secrets)
    return render_keepalived(snapshot, se_group_id, slot)
