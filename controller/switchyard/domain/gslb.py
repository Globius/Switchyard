# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Global server load balancing on PowerDNS: wide IPs and their records (ADR 0160, 0161).

A *wide IP* is a DNS name in a zone the tenant owns whose answer depends on which of its members are
healthy: ``www.example.com`` answers with the VIPs of the sites that are up. PowerDNS does the
answering and the health checking (LUA records: ``ifportup``, ``ifurlup``, ``pickwrandom``,
``view``);
the controller only decides what the records say. This module is pure: it validates a wide IP and
renders PowerDNS record sets.

Every value that reaches a Lua expression is a validated token (an IP literal, an integer, a short
word or a path with a fixed alphabet), so a tenant cannot write Lua of their own.

Example:
    >>> first = {"address": "192.0.2.10"}
    >>> second = {"address": "192.0.2.20", "priority": 2}
    >>> w = validate_wide_ip(
    ...     {"algorithm": "failover", "members": [first, second]}, resolve=lambda name: None
    ... )
    >>> print(render_rrsets("shop.example.test.", w)[0].records[0])
    A "ifportup(443, {{'192.0.2.10'}, {'192.0.2.20'}}, {timeout=1000})"
"""

import ipaddress
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ALGORITHMS = ("failover", "weighted", "all", "subnet", "geo", "latency")
#: A faster member must beat the current first choice by this share before the order changes.
LATENCY_MARGIN = 0.25
HEALTH_KINDS = ("tcp", "http")
MIN_TTL, MAX_TTL = 5, 3600
MAX_MEMBERS = 32

_LAB = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_LABEL = re.compile(rf"^(@|{_LAB}(\.{_LAB}){{0,3}})$")
_ZONE = re.compile(rf"^({_LAB}\.){{1,6}}[a-z]{{2,24}}$")
_PATH = re.compile(r"^/[A-Za-z0-9._~/%-]{0,200}$")
_MATCH = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")
_NAME = re.compile(r"^[a-z0-9]([a-z0-9_-]{0,61}[a-z0-9])?$")
ALL = "selector='all'"


class GslbInvalid(ValueError):
    """Invalid input; ``errors`` has a dotted path, a code and a message for every finding."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        """Build the error from the findings."""
        super().__init__("the request is invalid")
        self.errors = errors


@dataclass
class _Findings:
    errors: list[dict[str, str]] = field(default_factory=list)

    def add(self, path: str, code: str, message: str) -> None:
        self.errors.append({"path": path, "code": code, "message": message})

    def raise_if_any(self) -> None:
        if self.errors:
            raise GslbInvalid(self.errors)


def validate_zone_name(name: str) -> str:
    """A DNS zone: lowercase labels, 2 to 7 of them; a trailing dot is dropped.

    Example:
        >>> validate_zone_name("gslb.example.test.")
        'gslb.example.test'
    """
    value = name.rstrip(".").lower()
    if not _ZONE.match(value) or len(value) > 200:
        raise GslbInvalid([{"path": "name", "code": "invalid", "message": "not a DNS zone name"}])
    return value


def validate_label(label: str) -> str:
    """A name inside a zone: ``@`` (the apex) or up to four lowercase labels."""
    if not _LABEL.match(label):
        raise GslbInvalid(
            [{"path": "name", "code": "invalid", "message": "use @ or lowercase labels, a-z 0-9 -"}]
        )
    return label


def validate_wide_ip(
    raw: dict[str, Any], *, resolve: Callable[[str], str | None]
) -> dict[str, Any]:
    """Validate and normalise a wide IP's settings.

    ``resolve`` maps a virtual service name to its VIP (or ``None``): a member may name a service
    instead of an address, and the address is looked up whenever the records are rendered.

    Raises:
        GslbInvalid: With the path of every bad field (``members[1].port`` ...).
    """
    f = _Findings()
    algorithm = raw.get("algorithm", "failover")
    if algorithm not in ALGORITHMS:
        f.add("algorithm", "invalid", f"one of {', '.join(ALGORITHMS)}")
    ttl = raw.get("ttl", 30)
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not MIN_TTL <= ttl <= MAX_TTL:
        f.add("ttl", "invalid", f"whole seconds from {MIN_TTL} to {MAX_TTL}")
    health = _health(f, "health", raw.get("health") or {})
    members_raw = raw.get("members")
    members: list[dict[str, Any]] = []
    if not isinstance(members_raw, list) or not 1 <= len(members_raw) <= MAX_MEMBERS:
        f.add("members", "invalid", f"give 1 to {MAX_MEMBERS} members")
        members_raw = []
    for i, m in enumerate(members_raw):
        members.append(_member(f, f"members[{i}]", m, resolve, algorithm))
    for key in sorted(set(raw) - {"algorithm", "ttl", "health", "members"}):
        f.add(key, "unknown_field", "this field is not used")
    if algorithm == "weighted" and members and not any(m.get("weight", 1) > 0 for m in members):
        f.add("members", "invalid", "at least one member needs a weight above 0")
    f.raise_if_any()
    return {"algorithm": algorithm, "ttl": ttl, "health": health, "members": members}


def _health(f: _Findings, path: str, h: Any) -> dict[str, Any]:
    if not isinstance(h, dict):
        f.add(path, "invalid", "an object")
        return {}
    kind = h.get("kind", "tcp")
    if kind not in HEALTH_KINDS:
        f.add(f"{path}.kind", "invalid", "tcp or http")
    port = h.get("port", 443)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        f.add(f"{path}.port", "invalid", "a port from 1 to 65535")
    timeout = h.get("timeout_ms", 1000)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 100 <= timeout <= 5000:
        f.add(f"{path}.timeout_ms", "invalid", "milliseconds from 100 to 5000")
    out: dict[str, Any] = {"kind": kind, "port": port, "timeout_ms": timeout}
    if kind == "http":
        scheme = h.get("scheme", "http" if port == 80 else "https")  # port 80 speaks plain http
        if scheme not in ("http", "https"):
            f.add(f"{path}.scheme", "invalid", "http or https")
        p = h.get("path", "/")
        if not isinstance(p, str) or not _PATH.match(p):
            f.add(f"{path}.path", "invalid", "a path starting with /")
        out |= {"scheme": scheme, "path": p}
        match = h.get("match")
        if match is not None:
            if not isinstance(match, str) or not _MATCH.match(match):
                f.add(f"{path}.match", "invalid", "up to 64 letters, digits, space . _ -")
            out["match"] = match
        host = h.get("host")
        if host is not None:
            if not isinstance(host, str) or not _ZONE.match(host):
                f.add(f"{path}.host", "invalid", "a host name")
            out["host"] = host
    for key in sorted(set(h) - {"kind", "port", "scheme", "path", "match", "host", "timeout_ms"}):
        f.add(f"{path}.{key}", "unknown_field", "this field is not used")
    return out


def _member(
    f: _Findings, path: str, m: Any, resolve: Callable[[str], str | None], algorithm: Any
) -> dict[str, Any]:
    if not isinstance(m, dict):
        f.add(path, "invalid", "an object")
        return {}
    out: dict[str, Any] = {}
    address, service = m.get("address"), m.get("virtual_service")
    if (address is None) == (service is None):
        f.add(path, "invalid", "give either address or virtual_service")
    if address is not None:
        try:
            out["address"] = str(ipaddress.ip_address(str(address)))
        except ValueError:
            f.add(f"{path}.address", "invalid", "an IPv4 or IPv6 address")
    if service is not None:
        if not isinstance(service, str) or not _NAME.match(service):
            f.add(f"{path}.virtual_service", "invalid", "a virtual service name")
        else:
            out["virtual_service"] = service
            if resolve(service) is None:
                f.add(
                    f"{path}.virtual_service",
                    "unknown",
                    f"no virtual service {service!r} with a VIP",
                )
    site = m.get("site")
    if site is not None:
        if not isinstance(site, str) or not _NAME.match(site):
            f.add(f"{path}.site", "invalid", "a site name")
        out["site"] = site
    weight = m.get("weight", 1)
    if isinstance(weight, bool) or not isinstance(weight, int) or not 0 <= weight <= 1000:
        f.add(f"{path}.weight", "invalid", "a whole number from 0 to 1000")
    out["weight"] = weight
    priority = m.get("priority", 1)
    if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 10:
        f.add(f"{path}.priority", "invalid", "1 (first choice) to 10")
    out["priority"] = priority
    subnets = m.get("subnets", [])
    if algorithm == "subnet" or subnets:
        if not isinstance(subnets, list) or len(subnets) > 16:
            f.add(f"{path}.subnets", "invalid", "up to 16 CIDR blocks")
            subnets = []
        clean = []
        for j, s in enumerate(subnets):
            try:
                clean.append(str(ipaddress.ip_network(str(s), strict=False)))
            except ValueError:
                f.add(f"{path}.subnets[{j}]", "invalid", "a CIDR block")
        out["subnets"] = clean
    unknown = set(m) - {"address", "virtual_service", "site", "weight", "priority", "subnets"}
    for key in sorted(unknown):
        f.add(f"{path}.{key}", "unknown_field", "this field is not used")
    return out


@dataclass(frozen=True)
class Rrset:
    """One PowerDNS record set: a name, a type (``LUA``) and its record contents."""

    name: str
    type: str
    ttl: int
    records: list[str]


def _q(value: str) -> str:
    return "'" + value + "'"


def _ips(addresses: list[str]) -> str:
    return "{" + ", ".join(_q(a) for a in addresses) + "}"


def _check_call(health: dict[str, Any], addresses: str, options: str, fqdn: str) -> str:
    """``ifportup`` or ``ifurlup`` over ``addresses`` (already rendered as a Lua table)."""
    timeout = health.get("timeout_ms", 1000)
    options = f"{options}, timeout={timeout}" if options else f"timeout={timeout}"
    if health.get("kind") == "http":
        authority = health.get("host") or fqdn.rstrip(".")
        url = f"{health['scheme']}://{authority}:{health['port']}{health['path']}"
        opts = []
        if health.get("match"):
            opts.append(f"stringmatch='{health['match']}'")
        if options:
            opts.append(options)
        return f"ifurlup({_q(url)}, {addresses}, {{{', '.join(opts)}}})"
    return f"ifportup({health['port']}, {addresses}, {{{options}}})"


def next_order(
    current: list[int], ms: dict[int, float | None], margin: float = LATENCY_MARGIN
) -> list[int]:
    """The members ordered fastest first, without flapping.

    ``ms`` is the smoothed round-trip time per member index (``None``: not reachable). The current
    first choice keeps its place unless it is unreachable or the best member is at least ``margin``
    faster, so two members of similar speed do not trade places on every measurement. Members that
    cannot be reached go last.

    Example:
        >>> next_order([0, 1], {0: 52.0, 1: 48.0})   # 1 is only 8% faster: no change
        [0, 1]
        >>> next_order([0, 1], {0: 100.0, 1: 40.0})  # 60% faster: switch
        [1, 0]
        >>> next_order([0, 1], {0: None, 1: 40.0})
        [1, 0]
    """
    measured = sorted((i for i, v in ms.items() if v is not None), key=lambda i: (ms[i] or 0, i))
    dead = sorted(i for i, v in ms.items() if v is None)
    if not measured:
        return current or sorted(ms)
    first = measured[0]
    keep = current[0] if current and current[0] in measured else None
    if keep is not None and keep != first and (ms[first] or 0) > (ms[keep] or 0) * (1 - margin):
        first = keep
    return [first] + [i for i in measured if i != first] + dead


def render_rrsets(
    fqdn: str,
    wide: dict[str, Any],
    *,
    addresses: dict[int, str] | None = None,
    order: list[int] | None = None,
) -> list[Rrset]:
    """The record sets that make ``fqdn`` answer as the wide IP says.

    ``addresses`` maps a member's index to its address when it names a virtual service; members
    with an ``address`` need no entry. The one ``LUA`` record set has a record per address family
    (PowerDNS keeps all the LUA records of a name in one set).

    ``order`` is, for the ``latency`` algorithm, the member indexes fastest first (see
    :func:`next_order`); each rank becomes its own failover tier, and members not listed come last.

    Raises:
        GslbInvalid: A member names a service and no address was given for it.
    """
    members = []
    ranks = {i: r + 1 for r, i in enumerate(order or [])}
    for i, m in enumerate(wide["members"]):
        address = m.get("address") or (addresses or {}).get(i)
        if address is None:
            raise GslbInvalid(
                [{"path": f"members[{i}]", "code": "unresolved", "message": "no address"}]
            )
        member = {**m, "address": address}
        if wide["algorithm"] == "latency":
            member["priority"] = ranks.get(i, len(ranks) + 1)
        members.append(member)
    health = wide["health"] or {"kind": "tcp", "port": 443}
    records: list[str] = []
    for version, rtype in ((4, "A"), (6, "AAAA")):
        group = [m for m in members if ipaddress.ip_address(m["address"]).version == version]
        if group:
            expr = _expression(wide["algorithm"], group, health, fqdn)
            records.append(f'{rtype} "{expr}"')
    return [Rrset(fqdn, "LUA", wide["ttl"], records)]


def _expression(
    algorithm: str, group: list[dict[str, Any]], health: dict[str, Any], fqdn: str
) -> str:
    if algorithm == "all":
        return _check_call(health, _ips([m["address"] for m in group]), ALL, fqdn)
    if algorithm == "geo":
        everyone = _ips([m["address"] for m in group])
        return _check_call(health, everyone, "selector='pickclosest'", fqdn)
    if algorithm in ("failover", "latency"):
        tiers: dict[int, list[str]] = {}
        for m in sorted(group, key=lambda m: (m["priority"], m["address"])):
            tiers.setdefault(m["priority"], []).append(m["address"])
        nested = "{" + ", ".join(_ips(a) for _, a in sorted(tiers.items())) + "}"
        return _check_call(health, nested, "", fqdn)
    if algorithm == "weighted":
        # One check over every member that can be picked. A down set is answered with the whole set
        # (PowerDNS's backup), so the answer is the up members, or all of them when none is up.
        live = [m for m in group if m["weight"] > 0]
        pairs = ", ".join(f"{{{m['weight']}, {_q(m['address'])}}}" for m in live)
        up = _check_call(health, "{" + _ips([m["address"] for m in live]) + "}", ALL, fqdn)
        return (
            f";local all = {{{pairs}}} local up = {up} local ok = {{}} "
            f"for _, a in ipairs(up) do ok[tostring(a)] = true end local c = {{}} "
            f"for _, m in ipairs(all) do if ok[m[2]] then c[#c + 1] = m end end "
            f"if #c == 0 then c = all end return pickwrandom(c)"
        )
    # subnet: one view; members without subnets are the default answer
    parts = []
    default: list[str] = []
    for m in group:
        if m.get("subnets"):
            nets = ", ".join(_q(s) for s in m["subnets"])
            call = _check_call(health, _ips([m["address"]]), ALL, fqdn)
            parts.append("{{" + nets + "}, " + call + "}")
        else:
            default.append(m["address"])
    if default:
        call = _check_call(health, _ips(default), ALL, fqdn)
        parts.append("{{'0.0.0.0/0', '::/0'}, " + call + "}")
    return "view({" + ", ".join(parts) + "})"
