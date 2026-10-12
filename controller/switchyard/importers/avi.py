"""Convert an NSX ALB (Avi) configuration export into Switchyard objects (ADR 0150).

The converter is a pure function over the export's JSON: no network, no database, no secrets
handling beyond handing certificate PEMs back to the caller, which stores them in the secrets
backend. It reads the objects a migration needs (virtual services, VIPs, pools and pool groups,
health monitors, certificates, HTTP policy sets, profiles), follows references from the virtual
services so that unused objects stay behind, and reports every decision as a finding:

- ``info``: converted, with how;
- ``warning``: converted approximately (what was approximated);
- ``manual``: not converted, someone has to do it (WAF, DataScripts, external monitors ...);
- ``skipped``: left behind on purpose (unused, expired, no server addresses).

An SNI parent and its children become one HTTP service with a host route per child, the pattern
the platform replaces (design 01, "What the Avi backup shows"). VIPs do not carry over: the caller
supplies a function that gives every Avi VIP its new address.

Example:
    >>> ref_name("https://ctl/api/pool/pool-1f2e#web-pool")
    'web-pool'
    >>> ref_name("/api/pool?name=web-pool")
    'web-pool'
    >>> slug("Keycloak SSO (prod)")
    'keycloak-sso-prod'
"""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse
from uuid import UUID

from switchyard.ids import uuid7

Level = Literal["info", "warning", "manual", "skipped"]
#: What the caller supplies: ``(Avi VIP name, old address) -> new address`` (``None``: none).
VipFor = Callable[[str, str | None], str | None]

_MAX_PORT_RANGE = 16


@dataclass(frozen=True)
class Finding:
    """One decision of the conversion."""

    level: Level
    object: str
    message: str


@dataclass
class PlannedObject:
    """An object to create: ``kind`` is ``health_monitor``, ``pool`` or ``virtual_service``."""

    kind: str
    id: UUID
    name: str
    avi_name: str
    body: dict[str, Any]


@dataclass
class PlannedCertificate:
    """A certificate to import. ``key`` is ``None`` when the export holds no private key."""

    id: UUID
    name: str
    avi_name: str
    pem: str
    key: str | None
    sans: list[str]
    not_after: datetime | None


@dataclass
class VipNeed:
    """An Avi VIP and the services that share it."""

    avi_vsvip: str
    address: str | None
    services: list[str]
    new_address: str | None = None


@dataclass
class AviPlan:
    """What the conversion produced."""

    objects: list[PlannedObject] = field(default_factory=list)
    certificates: list[PlannedCertificate] = field(default_factory=list)
    vips: list[VipNeed] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def note(self, level: Level, obj: str, message: str) -> None:
        """Record a finding."""
        self.findings.append(Finding(level, obj, message))

    def counts(self) -> dict[str, int]:
        """Objects to create per kind (certificates included)."""
        out: dict[str, int] = {}
        for o in self.objects:
            out[o.kind] = out.get(o.kind, 0) + 1
        out["certificate"] = len(self.certificates)
        return out


@dataclass(frozen=True)
class AviOptions:
    """Choices of the person importing.

    Attributes:
        se_group_id: The SE group the services will use.
        vip_for: Gives each Avi VIP its new address.
        prefix: Put in front of every new name (``avi-``, for example).
        include_unused: Convert pools, monitors and certificates no service uses.
        only: Convert only these virtual services (by Avi name); empty means all.
        taken: Names already used in the tenant, per kind, so new names never collide.
        now: The clock, for expired certificates.
    """

    se_group_id: UUID
    vip_for: VipFor
    prefix: str = ""
    include_unused: bool = False
    only: frozenset[str] = frozenset()
    taken: Mapping[str, frozenset[str]] = field(default_factory=dict)
    now: datetime | None = None


# -- small helpers ---------------------------------------------------------------------------------


def ref_name(ref: str | None) -> str | None:
    """The object name inside an Avi reference (``...#name``, ``?name=`` or the last segment)."""
    if not ref:
        return None
    if "#" in ref:
        return ref.rsplit("#", 1)[1] or None
    parsed = urlparse(ref)
    names = parse_qs(parsed.query).get("name")
    if names:
        return names[0]
    return parsed.path.rstrip("/").rsplit("/", 1)[-1] or None


def ref_uuid(ref: str | None) -> str | None:
    """The uuid segment of a reference (``/api/pool/pool-1f2e...``), if it has one."""
    if not ref:
        return None
    path = urlparse(ref.split("#", 1)[0]).path.rstrip("/")
    last = path.rsplit("/", 1)[-1]
    return last if "-" in last else None


def slug(name: str) -> str:
    """A valid object name made from an Avi name.

    Example:
        >>> slug("Insure-A.example")
        'insure-a-example'
        >>> slug("---")
        'x'
    """
    out = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_")
    return (out[:63].rstrip("-_") or "x") if out else "x"


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


class _Index:
    """The export, indexed by type and by name and uuid."""

    def __init__(self, export: Mapping[str, Any]) -> None:
        self.by_type: dict[str, list[dict[str, Any]]] = {}
        for key, value in export.items():
            items = value.get("results", value) if isinstance(value, Mapping) else value
            if isinstance(items, list):
                self.by_type[_norm(key)] = [i for i in items if isinstance(i, dict)]
        self.names: dict[str, dict[str, dict[str, Any]]] = {}
        self.uuids: dict[str, dict[str, dict[str, Any]]] = {}
        for kind, items in self.by_type.items():
            for item in items:
                if item.get("name"):
                    self.names.setdefault(kind, {})[str(item["name"])] = item
                if item.get("uuid"):
                    self.uuids.setdefault(kind, {})[str(item["uuid"])] = item

    def all(self, kind: str) -> list[dict[str, Any]]:
        return self.by_type.get(kind, [])

    def get(self, kind: str, ref: str | None) -> dict[str, Any] | None:
        name, uid = ref_name(ref), ref_uuid(ref)
        if uid and uid in self.uuids.get(kind, {}):
            return self.uuids[kind][uid]
        if name and name in self.names.get(kind, {}):
            return self.names[kind][name]
        return None


class _Names:
    """Unique new names per kind."""

    def __init__(self, options: AviOptions) -> None:
        self._used: dict[str, set[str]] = {k: set(v) for k, v in options.taken.items()}
        self._prefix = options.prefix

    def new(self, kind: str, avi_name: str) -> str:
        base = slug(self._prefix + avi_name)
        used = self._used.setdefault(kind, set())
        name, n = base, 1
        while name in used:
            n += 1
            suffix = f"-{n}"
            name = base[: 63 - len(suffix)] + suffix
        used.add(name)
        return name


# -- the conversion --------------------------------------------------------------------------------

_ALGORITHMS = {
    "LB_ALGORITHM_ROUND_ROBIN": ("roundrobin", None),
    "LB_ALGORITHM_LEAST_CONNECTIONS": ("leastconn", None),
    "LB_ALGORITHM_CONSISTENT_HASH": ("source", "hashed on the client address"),
    "LB_ALGORITHM_LEAST_LOAD": ("leastconn", "least load approximated by least connections"),
    "LB_ALGORITHM_FEWEST_SERVERS": (
        "leastconn",
        "fewest servers approximated by least connections",
    ),
    "LB_ALGORITHM_FASTEST_RESPONSE": (
        "leastconn",
        "fastest response approximated by least connections",
    ),
}
_REDIRECT_CODES = {"301": 301, "302": 302, "303": 303, "307": 307, "308": 308}


def convert(export: Mapping[str, Any], options: AviOptions) -> AviPlan:
    """Convert an Avi configuration export. See the module docstring for the rules."""
    return _Converter(export, options).run()


class _Converter:
    def __init__(self, export: Mapping[str, Any], options: AviOptions) -> None:
        self.x = _Index(export)
        self.o = options
        self.plan = AviPlan()
        self.names = _Names(options)
        self.pool_ids: dict[str, UUID] = {}
        self.monitor_ids: dict[tuple[Any, ...], UUID] = {}
        self.cert_ids: dict[str, UUID | None] = {}
        self.vip_needs: dict[str, VipNeed] = {}
        self.used_pools: set[str] = set()
        self.used_certs: set[str] = set()
        self.used_monitors: set[str] = set()

    def run(self) -> AviPlan:
        services = [
            vs
            for vs in self.x.all("virtualservice")
            if not self.o.only or str(vs.get("name")) in self.o.only
        ]
        children: dict[str, list[dict[str, Any]]] = {}
        parents: list[dict[str, Any]] = []
        for vs in services:
            parent = ref_name(vs.get("vh_parent_vs_ref"))
            if self._kind(vs) == "child" and parent:
                children.setdefault(parent, []).append(vs)
            else:
                parents.append(vs)
        for vs in parents:
            self._service(vs, children.get(str(vs.get("name")), []))
        for kind in ("pool", "poolgroup"):
            for item in self.x.all(kind):
                if str(item.get("name")) not in self.used_pools and not self.o.include_unused:
                    self.plan.note(
                        "skipped", f"{kind}:{item.get('name')}", "no virtual service uses it"
                    )
        self.plan.vips = list(self.vip_needs.values())
        for need in self.plan.vips:
            need.new_address = self.o.vip_for(need.avi_vsvip, need.address)
        return self.plan

    # -- virtual services ---------------------------------------------------------------------

    @staticmethod
    def _kind(vs: Mapping[str, Any]) -> str:
        kind = str(vs.get("type") or vs.get("vh_type") or "")
        if kind.endswith("CHILD"):
            return "child"
        if kind.endswith("PARENT"):
            return "parent"
        if kind.endswith("ENHANCED"):
            return "enhanced"
        return "normal"

    def _profile(self, vs: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        app = self.x.get("applicationprofile", vs.get("application_profile_ref")) or {}
        net = self.x.get("networkprofile", vs.get("network_profile_ref")) or {}
        app_type = str(app.get("type", ""))
        net_type = str((net.get("profile") or {}).get("type", ""))
        if app_type.endswith("HTTP") or app_type.endswith("HTTPS"):
            return "http", app
        if "UDP" in net_type:
            return "udp", app
        return "tcp", app

    def _vip(self, vs: Mapping[str, Any], label: str) -> str | None:
        vsvip = self.x.get("vsvip", vs.get("vsvip_ref"))
        old: str | None = None
        group = ref_name(vs.get("vsvip_ref")) or str(vs.get("name"))
        if vsvip:
            vips = vsvip.get("vip") or []
            if vips:
                old = ((vips[0].get("ip_address") or {}).get("addr")) or None
            group = str(vsvip.get("name", group))
        elif isinstance(vs.get("ip_address"), dict):
            old = vs["ip_address"].get("addr")
        need = self.vip_needs.setdefault(group, VipNeed(group, old, []))
        need.services.append(label)
        return group

    def _service(self, vs: dict[str, Any], kids: list[dict[str, Any]]) -> None:
        avi_name = str(vs.get("name"))
        label = f"virtual_service:{avi_name}"
        protocol, app = self._profile(vs)
        group = self._vip(vs, avi_name)
        pool = self._pool_of(vs)
        routes: list[dict[str, Any]] = []
        default_pool = self._pool_id(pool, label) if pool else None
        certs = self._certs_of(vs, label)
        ports = self._ports(vs, app, protocol, label)
        for kid in kids:
            kid_pool = self._pool_of(kid)
            kid_certs = self._certs_of(kid, f"virtual_service:{kid.get('name')}")
            for cert in kid_certs:
                if cert not in certs:
                    certs.append(cert)
            domains = list(kid.get("vh_domain_name") or [])
            if not domains:
                self.plan.note(
                    "manual", f"virtual_service:{kid.get('name')}", "SNI child without domain names"
                )
            for domain in domains:
                if kid_pool is not None:
                    routes.append(
                        {
                            "order": 0,
                            "match": {"host": domain.lower()},
                            "pool_id": str(self._pool_id(kid_pool, label)),
                        }
                    )
            routes += self._policy_routes(kid, domain_list=domains)
            self.plan.note(
                "info",
                f"virtual_service:{kid.get('name')}",
                f"folded into {avi_name} as host routes",
            )
        routes += self._policy_routes(vs, domain_list=[])
        if self._kind(vs) == "enhanced":
            self.plan.note(
                "warning",
                label,
                "enhanced virtual hosting rules are converted from its policies only",
            )
        if protocol != "http" and (routes or kids):
            self.plan.note(
                "manual", label, "a TCP/UDP service cannot have routes; hosts were dropped"
            )
            routes = []
        if default_pool is None and not routes:
            self.plan.note("skipped", label, "no pool and no routing: nothing to serve")
            return
        for index, route in enumerate(routes):
            route["order"] = index
        https_redirect = self._take_https_redirect(routes)
        if https_redirect:
            for p in ports:
                if not p["tls"]:
                    p["https_redirect_port"] = next((q["port"] for q in ports if q["tls"]), 443)
        self._security_notes(vs, label)
        body: dict[str, Any] = {
            "se_group_id": str(self.o.se_group_id),
            "protocol": protocol,
            "enabled": bool(vs.get("enabled", True)),
            "vip": f"@vip:{group}",
            "ports": ports,
        }
        if protocol == "http":
            if routes:
                body["routes"] = routes
            if certs:
                body["certificate_ids"] = [str(c) for c in certs if c is not None]
        elif certs and protocol == "tcp":
            body["certificate_ids"] = [str(c) for c in certs if c is not None]
        if default_pool is not None:
            body["default_pool_id"] = str(default_pool)
        name = self.names.new("virtual_service", avi_name)
        self.plan.objects.append(PlannedObject("virtual_service", uuid7(), name, avi_name, body))
        self.plan.note(
            "info", label, f"-> {name} ({protocol}, {len(ports)} port(s), {len(routes)} route(s))"
        )

    def _pool_of(self, vs: Mapping[str, Any]) -> dict[str, Any] | None:
        pool = self.x.get("pool", vs.get("pool_ref"))
        if pool is not None:
            return pool
        group = self.x.get("poolgroup", vs.get("pool_group_ref"))
        if group is None:
            return None
        self.used_pools.add(str(group.get("name")))
        members = group.get("members") or []
        refs = [m.get("pool_ref") for m in members if m.get("pool_ref")]
        if not refs:
            return None
        if len(refs) > 1:
            self.plan.note(
                "warning",
                f"pool_group:{group.get('name')}",
                f"{len(refs)} pools in the group; only the first ({ref_name(refs[0])}) is used",
            )
        return self.x.get("pool", refs[0])

    def _ports(
        self, vs: Mapping[str, Any], app: Mapping[str, Any], protocol: str, label: str
    ) -> list[dict[str, Any]]:
        http2 = bool((app.get("http_profile") or {}).get("http2_enabled"))
        tls_profile = self._tls_profile(vs)
        ports: list[dict[str, Any]] = []
        for svc in vs.get("services") or []:
            first = int(svc.get("port", 0))
            last = int(svc.get("port_range_end") or first)
            if last - first + 1 > _MAX_PORT_RANGE:
                self.plan.note(
                    "manual",
                    label,
                    f"port range {first}-{last} is too wide; add the ports you need",
                )
                continue
            for number in range(first, last + 1):
                tls = bool(svc.get("enable_ssl"))
                port: dict[str, Any] = {"port": number, "tls": tls}
                if tls and protocol == "http":
                    port["http2"] = http2
                    if tls_profile:
                        port["tls_profile"] = tls_profile
                if svc.get("enable_proxy_protocol"):
                    port["proxy_protocol"] = True
                ports.append(port)
        if not ports:
            self.plan.note("manual", label, "no listening ports in the export")
            ports = [{"port": 80, "tls": False}]
        return ports

    def _tls_profile(self, vs: Mapping[str, Any]) -> str | None:
        profile = self.x.get("sslprofile", vs.get("ssl_profile_ref"))
        if not profile:
            return None
        versions = {
            str(v.get("type", "")).replace("SSL_VERSION_", "")
            for v in profile.get("accepted_versions") or []
        }
        if versions == {"TLS1_3"}:
            return "modern"
        if versions and not versions & {"SSL3", "TLS1", "TLS1_1"}:
            return "intermediate"
        return None

    def _security_notes(self, vs: Mapping[str, Any], label: str) -> None:
        if vs.get("waf_policy_ref"):
            self.plan.note(
                "manual",
                label,
                f"WAF policy {ref_name(vs['waf_policy_ref'])} is not converted (no WAF yet)",
            )
        for script in vs.get("vs_datascripts") or []:
            self.plan.note(
                "manual",
                label,
                f"DataScript {ref_name(script.get('vs_datascript_set_ref'))} has to be rewritten",
            )
        for key in ("bot_policy_ref", "security_policy_ref"):
            if vs.get(key):
                self.plan.note(
                    "manual",
                    label,
                    f"{key.replace('_ref', '')} {ref_name(vs[key])} is not converted",
                )

    # -- policies -----------------------------------------------------------------------------

    def _policy_routes(
        self, vs: Mapping[str, Any], *, domain_list: list[str]
    ) -> list[dict[str, Any]]:
        routes: list[dict[str, Any]] = []
        for attached in sorted(vs.get("http_policies") or [], key=lambda p: p.get("index", 0)):
            policy = self.x.get("httppolicyset", attached.get("http_policy_set_ref"))
            if policy is None:
                continue
            rules = (policy.get("http_request_policy") or {}).get("rules") or []
            for rule in sorted(rules, key=lambda r: r.get("index", 0)):
                route = self._rule(policy, rule, domain_list)
                if route is not None:
                    routes.append(route)
            for other in ("http_response_policy", "http_security_policy"):
                if (policy.get(other) or {}).get("rules"):
                    self.plan.note(
                        "manual",
                        f"http_policy:{policy.get('name')}",
                        f"{other} rules are not converted",
                    )
        return routes

    def _rule(
        self, policy: Mapping[str, Any], rule: Mapping[str, Any], domains: list[str]
    ) -> dict[str, Any] | None:
        where = f"http_policy:{policy.get('name')}/{rule.get('name', rule.get('index'))}"
        match = self._match(rule.get("match") or {}, domains, where)
        if match is None:
            return None
        redirect = rule.get("redirect_action")
        switching = rule.get("switching_action")
        unsupported = [
            k
            for k in rule
            if k.endswith("_action")
            and k not in {"redirect_action", "switching_action"}
            and rule[k]
        ]
        for key in unsupported:
            self.plan.note("manual", where, f"{key} is not converted")
        if match == {}:
            if (
                redirect
                and str(redirect.get("protocol", "")).upper() == "HTTPS"
                and not (redirect.get("host") or redirect.get("path"))
            ):
                self.plan.note("info", where, "HTTP to HTTPS redirect -> https_redirect_port")
                return {"order": 0, "match": {}, "redirect": {"location": "https://", "code": 301}}
            self.plan.note("manual", where, "a rule without a match cannot be a route")
            return None
        if redirect:
            location = self._location(redirect)
            if location is None:
                self.plan.note("manual", where, "redirect target cannot be expressed")
                return None
            code = _REDIRECT_CODES.get(
                re.sub(r"\D", "", str(redirect.get("status_code", "302"))), 302
            )
            self.plan.note("info", where, "redirect converted")
            return {"order": 0, "match": match, "redirect": {"location": location, "code": code}}
        if switching and switching.get("pool_ref"):
            pool = self.x.get("pool", switching["pool_ref"])
            if pool is None:
                self.plan.note("manual", where, "switching to an unknown pool")
                return None
            self.plan.note("info", where, "pool switch converted")
            return {"order": 0, "match": match, "pool_id": str(self._pool_id(pool, where))}
        if not unsupported:
            self.plan.note("manual", where, "rule has no redirect or pool action")
        return None

    def _match(
        self, match: Mapping[str, Any], domains: list[str], where: str
    ) -> dict[str, Any] | None:
        out: dict[str, Any] = {}
        path = match.get("path")
        if path:
            values = list(path.get("match_str") or [])
            criteria = str(path.get("match_criteria", ""))
            if len(values) != 1 or not values[0].startswith("/"):
                self.plan.note("manual", where, "path match needs one absolute path")
                return None
            if criteria.endswith("BEGINS_WITH"):
                out["path_prefix"] = values[0]
            elif criteria.endswith("EQUALS"):
                out["path_exact"] = values[0]
            else:
                self.plan.note("manual", where, f"path match {criteria or '?'} is not converted")
                return None
        host = match.get("host_hdr")
        if host:
            values = list(host.get("value") or [])
            if len(values) != 1 or not str(host.get("match_criteria", "")).endswith("EQUALS"):
                self.plan.note("manual", where, "host match needs one exact host")
                return None
            out["host"] = values[0].lower()
        elif len(domains) == 1 and out:
            out["host"] = domains[0].lower()
        for key in match:
            if key not in {"path", "host_hdr"} and match[key]:
                self.plan.note("manual", where, f"match on {key} is not converted")
                return None
        if not out:
            # an unconditional redirect to HTTPS is handled by the caller
            return {}
        return out

    @staticmethod
    def _location(redirect: Mapping[str, Any]) -> str | None:
        def text(part: Mapping[str, Any] | None) -> str | None:
            if not part:
                return None
            tokens = part.get("tokens") or []
            if any(t.get("type") != "URI_TOKEN_TYPE_STRING" for t in tokens):
                return None
            return "".join(str(t.get("str_value", "")) for t in tokens)

        path = text(redirect.get("path"))
        host = text(redirect.get("host"))
        if redirect.get("path") and path is None:
            return None
        if redirect.get("host") and host is None:
            return None
        if host:
            scheme = str(redirect.get("protocol", "HTTPS")).lower()
            port = redirect.get("port")
            suffix = f":{port}" if port and int(port) not in (80, 443) else ""
            return f"{scheme}://{host}{suffix}{path or '/'}"
        if path and path.startswith("/"):
            return path
        return None

    @staticmethod
    def _take_https_redirect(routes: list[dict[str, Any]]) -> bool:
        """Unconditional HTTP-to-HTTPS rules (empty match) become ``https_redirect_port``."""
        found = False
        for route in list(routes):
            if route.get("match") == {}:
                routes.remove(route)
                found = True
        return found

    # -- pools, monitors, certificates --------------------------------------------------------

    def _pool_id(self, pool: Mapping[str, Any], where: str) -> UUID:
        avi_name = str(pool.get("name"))
        self.used_pools.add(avi_name)
        if avi_name in self.pool_ids:
            return self.pool_ids[avi_name]
        object_id = uuid7()
        self.pool_ids[avi_name] = object_id
        label = f"pool:{avi_name}"
        algorithm, approx = _ALGORITHMS.get(str(pool.get("lb_algorithm", "")), ("roundrobin", None))
        if str(pool.get("lb_algorithm", "")) not in _ALGORITHMS and pool.get("lb_algorithm"):
            approx = f"{pool['lb_algorithm']} has no equivalent; round robin used"
        if approx:
            self.plan.note("warning", label, approx)
        default_port = pool.get("default_server_port")
        members: list[dict[str, Any]] = []
        ratios = {int(s.get("ratio", 1)) for s in pool.get("servers") or []}
        for server in pool.get("servers") or []:
            address = (server.get("ip") or {}).get("addr")
            if not address:
                self.plan.note(
                    "skipped", label, f"server {server.get('hostname', '?')} has no address"
                )
                continue
            member: dict[str, Any] = {"address": address}
            port = server.get("port") or default_port
            if port:
                member["port"] = int(port)
            if len(ratios) > 1:  # keep the proportions; the largest ratio gets the top weight
                member["weight"] = max(1, round(int(server.get("ratio", 1)) * 256 / max(ratios)))
            if server.get("enabled") is False:
                member["admin_state"] = "disabled"
            members.append(member)
        body: dict[str, Any] = {"algorithm": algorithm, "members": members}
        monitor = self._monitor_of(pool, label)
        if monitor is not None:
            body["health_monitor_id"] = str(monitor)
        if pool.get("ssl_profile_ref") or pool.get("ssl_key_and_certificate_ref"):
            body["tls"] = True
        persistence = self._persistence(pool, label)
        if persistence:
            body["persistence"] = persistence
        name = self.names.new("pool", avi_name)
        self.plan.objects.append(PlannedObject("pool", object_id, name, avi_name, body))
        self.plan.note("info", label, f"-> {name} ({len(members)} server(s))")
        return object_id

    def _persistence(self, pool: Mapping[str, Any], label: str) -> dict[str, Any] | None:
        profile = self.x.get(
            "applicationpersistenceprofile", pool.get("application_persistence_profile_ref")
        )
        if not profile:
            return None
        kind = str(profile.get("persistence_type", ""))
        if kind.endswith("CLIENT_IP_ADDRESS"):
            minutes = int(
                (profile.get("ip_persistence_profile") or {}).get("ip_persistent_timeout", 5)
            )
            return {"type": "source_ip", "timeout_s": max(1, minutes * 60)}
        self.plan.note(
            "warning",
            label,
            f"persistence {kind or '?'} is not converted here; set it on the new pool",
        )
        return None

    def _monitor_of(self, pool: Mapping[str, Any], label: str) -> UUID | None:
        for ref in pool.get("health_monitor_refs") or []:
            monitor = self.x.get("healthmonitor", ref)
            name = ref_name(ref) or "?"
            if monitor is None:
                # a system monitor that the export leaves out: assume the Avi defaults
                kind = "HEALTH_MONITOR_TCP" if "TCP" in name.upper() else "HEALTH_MONITOR_HTTP"
                monitor = {"name": name, "type": kind}
            return self._monitor_id(monitor, label)
        return None

    def _monitor_id(self, monitor: Mapping[str, Any], label: str) -> UUID | None:
        avi_name = str(monitor.get("name"))
        kind = str(monitor.get("type", ""))
        interval = int(monitor.get("send_interval", 10))
        timeout = int(monitor.get("receive_timeout", 4))
        rise = int(monitor.get("successful_checks", 2))
        fall = int(monitor.get("failed_checks", 2))
        port = monitor.get("monitor_port")
        body: dict[str, Any] = {
            "interval_s": max(1, min(300, interval)),
            "timeout_s": max(1, min(300, timeout)),
            "rise": max(1, rise),
            "fall": max(1, fall),
        }
        if port:
            body["port"] = int(port)
        where = f"health_monitor:{avi_name}"
        if kind in {"HEALTH_MONITOR_HTTP", "HEALTH_MONITOR_HTTPS"}:
            http = monitor.get("http_monitor") or {}
            request = str(http.get("http_request", "GET / HTTP/1.0")).split()
            check: dict[str, Any] = {
                "method": request[0]
                if request and request[0] in {"GET", "HEAD", "OPTIONS"}
                else "GET"
            }
            if (
                len(request) > 1
                and request[1].startswith("/")
                and re.fullmatch(r"/[A-Za-z0-9._~/%:@+,;=-]*", request[1])
            ):
                check["path"] = request[1]
            codes = sorted(
                {
                    200 if "2XX" in str(c) else 301 if "3XX" in str(c) else 0
                    for c in http.get("http_response_code") or []
                }
                - {0}
            )
            if codes:
                check["expect_status"] = codes
            body |= {"type": "http", "http": check}
            if kind == "HEALTH_MONITOR_HTTPS":
                self.plan.note(
                    "warning",
                    where,
                    "HTTPS check: the check runs over TLS only if the pool uses TLS",
                )
        elif kind == "HEALTH_MONITOR_TCP":
            body["type"] = "tcp"
        elif kind == "HEALTH_MONITOR_UDP":
            body["type"] = "udp"
        elif kind == "HEALTH_MONITOR_PING":
            body["type"] = "tcp"
            self.plan.note(
                "warning", where, "ICMP checks do not exist here; a TCP connect check is used"
            )
        else:
            self.plan.note("manual", where, f"{kind or 'unknown'} monitors are not converted")
            return None
        key = tuple(sorted((k, repr(v)) for k, v in body.items()))
        if key in self.monitor_ids:
            return self.monitor_ids[key]
        object_id = uuid7()
        self.monitor_ids[key] = object_id
        name = self.names.new("health_monitor", avi_name)
        self.plan.objects.append(PlannedObject("health_monitor", object_id, name, avi_name, body))
        self.plan.note("info", where, f"-> {name}")
        return object_id

    def _certs_of(self, vs: Mapping[str, Any], label: str) -> list[UUID]:
        out: list[UUID] = []
        for ref in vs.get("ssl_key_and_certificate_refs") or []:
            cert_id = self._cert_id(ref, label)
            if cert_id is not None:
                out.append(cert_id)
        if any(s.get("enable_ssl") for s in vs.get("services") or []) and not out:
            self.plan.note("manual", label, "TLS is enabled but no certificate could be converted")
        return out

    def _cert_id(self, ref: str, label: str) -> UUID | None:
        item = self.x.get("sslkeyandcertificate", ref)
        name = ref_name(ref) or "?"
        if item is None:
            self.plan.note("manual", label, f"certificate {name} is not in the export")
            return None
        avi_name = str(item.get("name"))
        if avi_name in self.cert_ids:
            return self.cert_ids[avi_name]
        where = f"certificate:{avi_name}"
        self.cert_ids[avi_name] = None
        if not str(item.get("type", "SSL_CERTIFICATE_TYPE_VIRTUALSERVICE")).endswith(
            "VIRTUALSERVICE"
        ):
            self.plan.note("skipped", where, "not a virtual service certificate")
            return None
        cert = item.get("certificate") or {}
        pem = cert.get("certificate")
        if not pem:
            self.plan.note("manual", where, "the export holds no certificate text")
            return None
        not_after = _parse_time(cert.get("not_after"))
        now = self.o.now or datetime.now(UTC)
        if not_after is not None and not_after < now:
            self.plan.note("skipped", where, f"expired on {not_after:%Y-%m-%d}")
            return None
        sans = [str(s).lower() for s in cert.get("subject_alt_names") or []]
        common = (cert.get("subject") or {}).get("common_name")
        if common and str(common).lower() not in sans:
            sans.append(str(common).lower())
        key = item.get("key")
        if not key:
            self.plan.note(
                "manual", where, "the private key is not in the export: upload the certificate"
            )
        object_id = uuid7()
        self.cert_ids[avi_name] = object_id
        name = self.names.new("certificate", avi_name)
        self.plan.certificates.append(
            PlannedCertificate(
                object_id, name, avi_name, str(pem), str(key) if key else None, sans, not_after
            )
        )
        self.plan.note("info", where, f"-> {name}" + ("" if key else " (no key)"))
        return object_id


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("T", " ").replace("Z", "").split(".")[0]
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def planned_ops_order(objects: Iterable[PlannedObject]) -> list[PlannedObject]:
    """Creation order: monitors, pools, then services (references first)."""
    rank = {"health_monitor": 0, "pool": 1, "virtual_service": 2}
    return sorted(objects, key=lambda o: rank.get(o.kind, 9))
