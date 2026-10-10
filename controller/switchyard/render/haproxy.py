"""Render an SE group's desired state to HAProxy configuration files.

:func:`render_haproxy` is a pure function: the same :class:`~switchyard.domain.snapshot.Snapshot`
always yields byte-identical files. Everything is sorted (frontends by service name, backends by
pool name, servers by server name, map and list lines by key) and nothing reads the clock, the
environment or the network.

Files produced (paths are relative to ``/etc/switchyard/`` on the SE, the only directory the agent
and the Data Plane API write to):

=============================== ===================================================================
``haproxy/haproxy.cfg``         global, defaults, one frontend per service, one backend per pool
``certs/<vs>.crtlist``          certificates and their SNI filters for a service's TLS ports
``haproxy/maps/<vs>-host.map``  exact host -> backend, for host-only routes
``haproxy/maps/<vs>-wild.map``  ``*.`` wildcard host regex -> backend, for host-only routes
``haproxy/lists/<policy>.cidr`` source networks of an allow-list policy set
=============================== ===================================================================

Certificate and key material is *not* rendered: ``crt-list`` entries point at
``/etc/switchyard/certs/<certificate-name>.pem``, which the agent writes from OpenBao.

``haproxy/haproxy.cfg`` carries ``# switchyard-render: sha256:<hex>``, a hash of every file. Drift
detection compares it with what an SE reports. The crt-list lives beside the certificates because
the Data Plane API stores both in one directory, and object names contain no dots because the API
rewrites dots in file names.

Request handling order inside an HTTP frontend, which the output makes explicit:

1. connection filter (allow list), client-IP restoration, header hygiene;
2. HTTP to HTTPS redirects, then policy-set redirects, then redirect routes;
3. path-qualified pool routes, in route ``order``;
4. host-only pool routes: exact host first, then single-label wildcard hosts;
5. the default pool, if any.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from jinja2 import Environment, PackageLoader, StrictUndefined

from switchyard.domain import models as dm
from switchyard.domain.snapshot import Snapshot, ensure_valid
from switchyard.render import escape as esc
from switchyard.render.escape import RenderError

ROOT_DIR = "/etc/switchyard"
BASE_DIR = "/etc/switchyard/haproxy"
CONFIG_PATH = "haproxy/haproxy.cfg"
CERT_DIR = "/etc/switchyard/certs"
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

# The output is configuration text, not HTML, so HTML autoescaping would corrupt it. Safety comes
# from the strict validators and from ``switchyard.render.escape``, applied before any value is
# interpolated.
_ENV = Environment(
    loader=PackageLoader("switchyard.render", "templates"),
    undefined=StrictUndefined,
    autoescape=False,  # noqa: S701
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
)


@dataclass(frozen=True)
class RenderWarning:
    """A structured renderer warning, as shown in a change set's diff.

    Attributes:
        code: Stable identifier (``udp_not_served``).
        path: Dotted path of the object concerned (``virtual_services.dns``).
        message: Human-readable text (the same text ``RenderResult.warnings`` carries).
    """

    code: str
    path: str
    message: str


@dataclass(frozen=True)
class RenderResult:
    """Files produced for one SE group.

    Attributes:
        files: Relative path to file content, sorted by path.
        warnings: Things the renderer deliberately did not render (for example UDP services).
        notices: The same warnings with a code and a path, for API consumers.
    """

    files: Mapping[str, str]
    warnings: tuple[str, ...] = ()
    notices: tuple[RenderWarning, ...] = ()

    @property
    def content_hash(self) -> str:
        """SHA-256 over sorted ``(path, content)`` pairs; equal hashes mean equal bytes."""
        return _digest(self.files)

    @property
    def fingerprint(self) -> str:
        """The fingerprint embedded in ``haproxy.cfg`` (what an SE reports it is running)."""
        value = fingerprint_of(self.files[CONFIG_PATH])
        if value is None:  # pragma: no cover - render always embeds it
            raise RenderError("rendered haproxy.cfg has no fingerprint")
        return value


def render_haproxy(snapshot: Snapshot, se_group_id: UUID) -> RenderResult:
    """Render the HAProxy files of one SE group.

    The snapshot is reduced to what the group references and validated first, so a render never
    starts from state the domain rules reject.

    Args:
        snapshot: Desired state; may contain other SE groups and tenants.
        se_group_id: The group to render.

    Returns:
        The files and any warnings.

    Raises:
        RenderError: If the group does not exist or a value cannot be rendered safely.
        switchyard.domain.errors.DomainValidationError: If the snapshot breaks a domain rule.

    Example:
        >>> from uuid import UUID
        >>> from switchyard.domain.loader import snapshot_from_mapping, symbolic_uuid
        >>> snap = snapshot_from_mapping({
        ...     "tenants": [{"id": "t", "name": "acme"}],
        ...     "networks": [{"id": "n", "tenant_id": "t", "name": "lb", "cidr": "192.0.2.0/24"}],
        ...     "se_groups": [{"id": "g", "tenant_id": "t", "name": "edge", "ha_mode": "vrrp",
        ...                    "networks": [{"network_id": "n", "role": "vip"}]}],
        ...     "pools": [{"id": "p", "tenant_id": "t", "name": "k8s",
        ...                "members": [{"address": "10.0.0.5", "port": 6443}]}],
        ...     "virtual_services": [{"id": "v", "tenant_id": "t", "se_group_id": "g",
        ...                           "name": "api", "protocol": "tcp", "vip": "192.0.2.10",
        ...                           "ports": [{"port": 6443}], "default_pool_id": "p"}],
        ... })
        >>> result = render_haproxy(snap, UUID(symbolic_uuid("g")))
        >>> list(result.files)
        ['haproxy/haproxy.cfg']
        >>> "    bind 192.0.2.10:6443" in result.files["haproxy/haproxy.cfg"]
        True
    """
    try:
        view = snapshot.for_se_group(se_group_id)
    except KeyError:
        raise RenderError(f"unknown SE group {se_group_id}") from None
    ensure_valid(view)
    return _Renderer(view).render()


_PLACEHOLDER = "0" * 64
FINGERPRINT_LINE = re.compile(r"^# switchyard-render: sha256:([0-9a-f]{64})$", re.MULTILINE)


def _digest(files: Mapping[str, str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(files[path].encode())
        digest.update(b"\0")
    return digest.hexdigest()


def fingerprint_of(config: str) -> str | None:
    r"""Return the render fingerprint embedded in a ``haproxy.cfg``, or ``None`` if absent.

    The fingerprint survives a round trip through the Data Plane API (which rewrites the file but
    keeps comments), so it identifies what an SE is running without comparing raw bytes.

    Example:
        >>> line = "# switchyard-render: sha256:" + "ab" * 32
        >>> fingerprint_of("# header\n" + line + "\nglobal\n") == "ab" * 32
        True
        >>> fingerprint_of("global") is None
        True
    """
    match = FINGERPRINT_LINE.search(config)
    return match.group(1) if match else None


def _need[T](value: T | None, what: str) -> T:
    """Narrow an optional the domain guarantees is set; fail the render if it is not."""
    if value is None:
        raise RenderError(f"missing {what}")
    return value


def _allow_list(policy: object) -> dm.AllowListPolicySet:
    if not isinstance(policy, dm.AllowListPolicySet):
        raise RenderError("expected an allow-list policy set")
    return policy


def _redirect_set(policy: object) -> dm.RedirectPolicySet:
    if not isinstance(policy, dm.RedirectPolicySet):
        raise RenderError("expected a redirect policy set")
    return policy


def _is_wild(host: str) -> bool:
    return host.startswith("*.")


def _quote_regex(regex: str) -> str:
    """Single-quote a regex so ``$`` is never read as environment-variable expansion."""
    if "'" in regex:
        raise RenderError(f"unsafe regex {regex!r}")
    return f"'{regex}'"


def _map_text(entries: Mapping[str, str]) -> str:
    return "".join(f"{key} {entries[key]}\n" for key in sorted(entries))


def _map_rule(converter: str, path: str) -> str:
    """Route by map lookup.

    The ``found`` guard matters: an empty lookup result would otherwise end rule evaluation and
    skip every later route.
    """
    lookup = f"var(txn.host),{converter}({path})"
    return f"use_backend %[{lookup}] if {{ {lookup} -m found }}"


class _Renderer:
    def __init__(self, snapshot: Snapshot) -> None:
        self.snap = snapshot
        self.group = snapshot.se_groups[0]
        self.pools = {p.id: p for p in snapshot.pools}
        self.monitors = {m.id: m for m in snapshot.health_monitors}
        self.certs = {c.id: c for c in snapshot.certificates}
        self.policies = {p.id: p for p in snapshot.policy_sets}
        self.files: dict[str, str] = {}
        self.warnings: list[str] = []
        self.notices: list[RenderWarning] = []
        self.pool_modes: dict[UUID, str] = {}

    def render(self) -> RenderResult:
        services = sorted(
            (s for s in self.snap.virtual_services if s.enabled), key=lambda s: s.name
        )
        frontends: list[dict[str, Any]] = []
        for svc in services:
            if svc.protocol is dm.Protocol.UDP:
                message = (
                    f"virtual service {svc.name}: udp is rendered by the keepalived renderer, "
                    "not haproxy.cfg"
                )
                self.warnings.append(message)
                self.notices.append(
                    RenderWarning("udp_not_served", f"virtual_services.{svc.name}", message)
                )
                continue
            mode = "http" if svc.protocol is dm.Protocol.HTTP else "tcp"
            for pool_id in self._pool_ids(svc):
                self.pool_modes[pool_id] = mode
            frontends.append(self._frontend(svc, mode))
        backends = [self._backend(self.pools[pid], mode) for pid, mode in self.pool_modes.items()]
        backends.sort(key=lambda b: b["name"])
        template = _ENV.get_template("haproxy.cfg.j2")

        def cfg(fingerprint: str) -> str:
            return template.render(
                group=esc.ident(self.group.name),
                maxconn=self.group.maxconn,
                frontends=frontends,
                backends=backends,
                fingerprint=fingerprint,
            )

        # The fingerprint covers every file; the config is hashed with a placeholder in its own
        # fingerprint line, so the line can carry the result without a circular definition.
        draft = {**self.files, CONFIG_PATH: cfg(_PLACEHOLDER)}
        self.files[CONFIG_PATH] = cfg(_digest(draft))
        return RenderResult(
            files=dict(sorted(self.files.items())),
            warnings=tuple(self.warnings),
            notices=tuple(self.notices),
        )

    # -- helpers ---------------------------------------------------------------------------------

    @staticmethod
    def _pool_ids(svc: dm.VirtualService) -> list[UUID]:
        ids = [r.pool_id for r in svc.routes if r.pool_id]
        if svc.default_pool_id:
            ids.append(svc.default_pool_id)
        return list(dict.fromkeys(ids))

    def _backend_name(self, pool_id: UUID) -> str:
        return "pool_" + esc.ident(self.pools[pool_id].name)

    @staticmethod
    def _list_path(policy: dm.AllowListPolicySet) -> str:
        return f"{BASE_DIR}/lists/{esc.ident(policy.name)}.cidr"

    # -- frontends -------------------------------------------------------------------------------

    def _frontend(self, svc: dm.VirtualService, mode: str) -> dict[str, Any]:
        name = esc.ident(svc.name)
        ports = sorted(svc.ports, key=lambda p: p.port)
        if any(p.tls for p in ports):
            self._crt_list(svc, name)
        allow = _allow_list(self.policies[svc.allow_list_id]) if svc.allow_list_id else None
        connection_rules: list[str] = []
        if allow is not None:
            self.files[f"haproxy/lists/{esc.ident(allow.name)}.cidr"] = self._allow_list_text(allow)
            if svc.enforce_allow_list:
                lst = self._list_path(allow)
                connection_rules.append(f"tcp-request session reject if !{{ src -f {lst} }}")
        request_rules: list[str] = []
        routing_rules: list[str] = []
        if mode == "http":
            request_rules = self._request_rules(svc, ports, allow)
            routing_rules = self._routing_rules(svc, name)
        return {
            "name": name,
            "mode": mode,
            "binds": [self._bind(svc, p, name, mode) for p in ports],
            "connection_rules": connection_rules,
            "request_rules": request_rules,
            "routing_rules": routing_rules,
            "default_backend": (
                self._backend_name(svc.default_pool_id) if svc.default_pool_id else None
            ),
        }

    @staticmethod
    def _bind(svc: dm.VirtualService, port: dm.VsPort, name: str, mode: str) -> str:
        parts = [f"bind {esc.bind_address(svc.vip, port.port)}"]
        if port.tls:
            parts.append(f"ssl crt-list {CERT_DIR}/{name}.crtlist")
            if mode == "http":
                parts.append("alpn h2,http/1.1" if port.http2 else "alpn http/1.1")
        if port.proxy_protocol:
            parts.append("accept-proxy")
        return " ".join(parts)

    def _crt_list(self, svc: dm.VirtualService, name: str) -> None:
        lines = []
        for cid in svc.certificate_ids:  # the first certificate is the default for unknown SNI
            cert = self.certs[cid]
            snis = " ".join(esc.arg(san) for san in sorted(cert.sans))
            lines.append(f"{CERT_DIR}/{esc.ident(cert.name)}.pem {snis}")
        self.files[f"certs/{name}.crtlist"] = "\n".join(lines) + "\n"

    @staticmethod
    def _allow_list_text(policy: dm.AllowListPolicySet) -> str:
        nets = sorted(policy.cidrs, key=lambda n: (n.version, int(n.network_address), n.prefixlen))
        return "".join(f"{n}\n" for n in nets)

    def _request_rules(
        self,
        svc: dm.VirtualService,
        ports: list[dm.VsPort],
        allow: dm.AllowListPolicySet | None,
    ) -> list[str]:
        rules: list[str] = []
        # Client address and header hygiene. The trusted flag is computed first because
        # ``set-src`` changes what ``src`` means for every later rule.
        if allow is not None and svc.restore_client_ip_header:
            header = esc.header_name(svc.restore_client_ip_header)
            lst = self._list_path(allow)
            rules += [
                f"http-request set-var(txn.trusted) bool(true) if {{ src -f {lst} }}",
                f"http-request set-src hdr({header}) "
                f"if {{ var(txn.trusted) -m bool }} {{ hdr({header}) -m found }}",
                f"http-request del-header {header} if !{{ var(txn.trusted) -m bool }}",
            ]
        rules += [
            "http-request del-header X-Forwarded-For",
            "http-request set-header X-Forwarded-Proto https if { ssl_fc }",
            "http-request set-header X-Forwarded-Proto http if !{ ssl_fc }",
            "http-request set-var(txn.host) req.hdr(host),field(1,:),lower",
        ]
        for port in ports:
            if port.https_redirect_port is not None:
                suffix = "" if port.https_redirect_port == 443 else f":{port.https_redirect_port}"
                rules.append(
                    f'http-request redirect prefix "https://%[var(txn.host)]{suffix}" code 301 '
                    f"if {{ dst_port {port.port} }}"
                )
        for match, redirect in self._redirects(svc):
            cond = " ".join(self._conditions(match))
            location = esc.location(redirect.location)
            rules.append(
                f"http-request redirect location {location} code {redirect.code} if {cond}"
            )
        return rules

    def _redirects(self, svc: dm.VirtualService) -> list[tuple[dm.RouteMatch, dm.RedirectAction]]:
        out: list[tuple[dm.RouteMatch, dm.RedirectAction]] = []
        for pid in svc.policy_set_ids:
            out += [(r.match, r.redirect) for r in _redirect_set(self.policies[pid]).rules]
        ordered = sorted(enumerate(svc.routes), key=lambda ir: (ir[1].order, ir[0]))
        out += [(r.match, r.redirect) for _, r in ordered if r.redirect is not None]
        return out

    @staticmethod
    def _conditions(match: dm.RouteMatch) -> list[str]:
        conds: list[str] = []
        if match.host:
            if _is_wild(match.host):
                regex = _quote_regex(esc.wildcard_regex(match.host))
                conds.append(f"{{ var(txn.host) -m reg {regex} }}")
            else:
                conds.append(f"{{ var(txn.host) -m str {esc.hostname(match.host)} }}")
        if match.path_prefix:
            conds.append(f"{{ path_beg {esc.arg(match.path_prefix)} }}")
        if match.path_exact:
            conds.append(f"{{ path {esc.arg(match.path_exact)} }}")
        return conds

    def _routing_rules(self, svc: dm.VirtualService, name: str) -> list[str]:
        rules: list[str] = []
        pool_routes = [(i, r) for i, r in enumerate(svc.routes) if r.pool_id is not None]
        path_routes = [(i, r) for i, r in pool_routes if r.match.path_prefix or r.match.path_exact]
        for _, route in sorted(path_routes, key=lambda ir: (ir[1].order, ir[0])):
            cond = " ".join(self._conditions(route.match))
            backend = self._backend_name(_need(route.pool_id, "route pool"))
            rules.append(f"use_backend {backend} if {cond}")
        exact: dict[str, str] = {}
        wild: dict[str, str] = {}
        for _, route in pool_routes:
            host = route.match.host
            if host is None or route.match.path_prefix or route.match.path_exact:
                continue
            target = self._backend_name(_need(route.pool_id, "route pool"))
            if _is_wild(host):
                wild[esc.wildcard_regex(host)] = target
            else:
                exact[esc.hostname(host)] = target
        if exact:
            self.files[f"haproxy/maps/{name}-host.map"] = _map_text(exact)
            rules.append(_map_rule("map", f"{BASE_DIR}/maps/{name}-host.map"))
        if wild:
            self.files[f"haproxy/maps/{name}-wild.map"] = _map_text(wild)
            rules.append(_map_rule("map_reg", f"{BASE_DIR}/maps/{name}-wild.map"))
        return rules

    # -- backends --------------------------------------------------------------------------------

    def _backend(self, pool: dm.Pool, mode: str) -> dict[str, Any]:
        monitor = self.monitors.get(pool.health_monitor_id) if pool.health_monitor_id else None
        lines: list[str] = [f"mode {mode}", f"balance {pool.algorithm.value}"]
        if pool.persistence is not None:
            lines += [
                f"stick-table type ipv6 size {pool.persistence.table_size} "
                f"expire {pool.persistence.timeout_s}s",
                "stick on src",
            ]
        if monitor is not None:
            lines += self._monitor_lines(monitor)
        servers = sorted(self._server(pool, m, monitor, mode) for m in pool.members)
        return {"name": self._backend_name(pool.id), "lines": lines, "servers": servers}

    @staticmethod
    def _monitor_lines(monitor: dm.HealthMonitor) -> list[str]:
        lines: list[str] = []
        if monitor.type is dm.MonitorType.HTTP:
            http = _need(monitor.http, "http monitor block")
            lines.append("option httpchk")
            send = f"http-check send meth {http.method} uri {esc.arg(http.path)}"
            if http.host:
                send += f" ver HTTP/1.1 hdr Host {esc.hostname(http.host)}"
            lines.append(send)
            if http.expect_status:
                codes = ",".join(str(c) for c in sorted(set(http.expect_status)))
                lines.append(f"http-check expect status {codes}")
        lines += [
            f"timeout check {monitor.timeout_s}s",
            f"default-server inter {monitor.interval_s}s fall {monitor.fall} rise {monitor.rise}",
        ]
        return lines

    @staticmethod
    def _server(
        pool: dm.Pool, member: dm.PoolMember, monitor: dm.HealthMonitor | None, mode: str
    ) -> str:
        address = member.address
        target = str(address) if address.version == 4 else f"ipv6@{address}"
        if member.port is not None:
            target += f":{member.port}"
        parts = [f"server {esc.ident(member.server_name)} {target}"]
        if monitor is not None:
            parts.append("check")
            if monitor.port is not None:
                parts.append(f"port {monitor.port}")
            if pool.tls:
                parts.append("check-ssl")
        if pool.tls:
            parts.append(
                f"ssl verify required ca-file {CA_BUNDLE}" if pool.tls_verify else "ssl verify none"
            )
            if mode == "http":
                parts.append("sni req.hdr(host),field(1,:),lower")
        parts.append(f"weight {member.weight}")
        if member.admin_state is not dm.AdminState.ENABLED:
            parts.append("disabled")  # drain and disabled both start out of rotation
        return " ".join(parts)
