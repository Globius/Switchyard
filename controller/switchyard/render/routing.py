"""Render the routing side of an SE group: ``keepalived.conf`` (VRRP) or ``frr.conf`` (BGP).

Like the HAProxy renderer these are pure functions of a snapshot (plus, for BGP session passwords,
the secrets the caller resolved from OpenBao). Output differs per engine only in its name, its VRRP
priority and its BGP router id, so every engine of a group is rendered separately.

Files (relative to ``/etc/switchyard/``, delivered to the agent as a signed bundle):

======================================= ===========================================================
``keepalived/keepalived.conf``          VRRP instance, health tracking, VIPs, static routes
``frr/frr.conf``                        BGP neighbours, BFD, VIP announcements, static routes
``frr/vips.json``                       ASN and VIP prefixes for the agent's route-health watchdog
======================================= ===========================================================

BGP announces only the group's VIPs (an out route-map) and accepts nothing from the peers.
"""

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

_ENV = Environment(
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
        router_id: IPv4 address used as BGP router id (the engine's management address).
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
    vips = [f"{vip}/{_vip_prefixlen(view, group, vip)}" for vip in _vips(view, group)]
    if not vips:
        raise RenderError(f"SE group {group.name} has no virtual service to announce")
    text = _ENV.get_template("keepalived.conf.j2").render(
        group=esc.ident(group.name),
        engine=esc.ident(slot.name),
        instance="sy_" + esc.ident(group.name),
        interface=esc.ident(profile.interface.lower()),
        vrid=profile.vrid,
        priority=max(1, profile.base_priority - profile.priority_step * slot.index),
        advert=profile.advert_interval_s,
        vips=vips,
        routes=[
            f"{line} dev {esc.ident(profile.interface)}"
            for line in _route_lines(profile.static_routes, "keepalived")
        ],
    )
    return {"keepalived/keepalived.conf": text}


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
    try:
        router_id = str(ipaddress.IPv4Address(slot.router_id))
    except ValueError:
        raise RenderError(f"engine {slot.name}: router_id must be an IPv4 address") from None
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
    v4 = [f"{v}/32" for v in vips if v.version == 4]
    v6 = [f"{v}/128" for v in vips if v.version == 6]
    text = _ENV.get_template("frr.conf.j2").render(
        group=esc.ident(group.name),
        engine=esc.ident(slot.name),
        asn=profile.asn,
        router_id=router_id,
        peers=peers,
        bfd=profile.bfd,
        v4_vips=v4,
        v6_vips=v6,
        v4_peers=[str(p.peer_ip) for p in ordered if p.peer_ip.version == 4],
        v6_peers=[str(p.peer_ip) for p in ordered if p.peer_ip.version == 6],
        max_paths=profile.max_paths,
        routes=_route_lines(profile.static_routes, "frr"),
    )
    watchdog = json.dumps({"asn": profile.asn, "prefixes": v4}, sort_keys=True, indent=2) + "\n"
    return {"frr/frr.conf": text, "frr/vips.json": watchdog}


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
