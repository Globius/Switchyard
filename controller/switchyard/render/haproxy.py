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
#: Port of the Prometheus exporter listener (8404 is the local health endpoint).
EXPORTER_PORT = 8405
CERT_DIR = "/etc/switchyard/certs"
#: Answers to ACME HTTP-01 challenges, keyed by request path. The file is empty in the rendered
#: configuration; the controller adds entries at run time while a certificate is being validated.
ACME_MAP_FILE = "haproxy/maps/acme-http01.map"
ACME_MAP = f"{BASE_DIR}/maps/acme-http01.map"
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

#: Protocol-aware health checks HAProxy ships, by monitor ``preset`` (ADR 0108).
_TCP_PRESETS = {
    "redis": "option redis-check",
    "smtp": "option smtpchk",
    "ldap": "option ldap-check",
    "mysql": "option mysql-check",
}

#: TLS 1.3 cipher suites (Mozilla server-side TLS, 5.7).
_TLS13_SUITES = "TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256"
#: TLS 1.2 ciphers of the ``intermediate`` profile (forward secrecy, AEAD).
_INTERMEDIATE_CIPHERS = (
    "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
    "DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:DHE-RSA-CHACHA20-POLY1305"
)
#: Extra ciphers of the ``legacy`` profile, for clients without AEAD support.
_LEGACY_CIPHERS = (
    _INTERMEDIATE_CIPHERS + ":ECDHE-ECDSA-AES128-SHA256:ECDHE-RSA-AES128-SHA256:"
    "ECDHE-ECDSA-AES128-SHA:ECDHE-RSA-AES128-SHA:ECDHE-ECDSA-AES256-SHA384:"
    "ECDHE-RSA-AES256-SHA384:ECDHE-ECDSA-AES256-SHA:ECDHE-RSA-AES256-SHA:"
    "AES128-GCM-SHA256:AES256-GCM-SHA384:AES128-SHA256:AES256-SHA256:AES128-SHA:AES256-SHA"
)
#: Bind options per profile: minimum version, TLS 1.2 ciphers, TLS 1.3 suites.
TLS_PROFILES: dict[dm.TlsProfile, tuple[str, str | None, str]] = {
    dm.TlsProfile.MODERN: ("TLSv1.3", None, _TLS13_SUITES),
    dm.TlsProfile.INTERMEDIATE: ("TLSv1.2", _INTERMEDIATE_CIPHERS, _TLS13_SUITES),
    dm.TlsProfile.LEGACY: ("TLSv1.0", _LEGACY_CIPHERS, _TLS13_SUITES),
}

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
        code: Stable identifier of the warning.
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


def _cookie_value(pool: dm.Pool, member: dm.PoolMember) -> str:
    """Opaque, stable cookie value of a member (ADR 0105).

    The server name often encodes the member's address, which must not reach clients, so the value
    is a short hash of the pool and member ids.

    Example:
        >>> import uuid
        >>> pool = dm.Pool(id=uuid.UUID(int=1), tenant_id=uuid.UUID(int=2), name="p")
        >>> member = dm.PoolMember(id=uuid.UUID(int=3), address="10.0.0.5")
        >>> _cookie_value(pool, member)
        'm0d1b1f77b038'
    """
    digest = hashlib.sha256(f"{pool.id}/{member.id}".encode()).hexdigest()
    return f"m{digest[:12]}"


def _map_rule(converter: str, path: str) -> str:
    """Route by map lookup.

    The ``found`` guard matters: an empty lookup result would otherwise end rule evaluation and
    skip every later route.
    """
    lookup = f"var(txn.host),{converter}({path})"
    return f"use_backend %[{lookup}] if {{ {lookup} -m found }}"


_ACME_MAP_TEXT = "# ACME HTTP-01 answers are added at run time (Switchyard).\n"


def _acme_rule() -> str:
    """Answer ``/.well-known/acme-challenge/<token>`` on port 80 from the challenge map.

    The map holds the full request path as key, so only a published token is ever answered.
    """
    lookup = f"path,map({ACME_MAP})"
    return (
        "http-request return status 200 content-type text/plain "
        f'lf-string "%[{lookup}]" if {{ dst_port 80 }} {{ {lookup} -m found }}'
    )


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
                continue  # IPVS serves it; the routing renderer writes it to keepalived.conf
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
                exporter=self._exporter(),
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

    def _exporter(self) -> dict[str, Any] | None:
        """Where the Prometheus exporter listens: port 8405, only for the management networks.

        The configuration is the same on every engine of the group, so it cannot name one
        engine's management address. The listener binds the wildcard and refuses any request whose
        destination address is outside the group's ``mgmt`` networks. A group without a
        management network gets no exporter listener.
        """
        networks = {n.id: n for n in self.snap.networks}
        cidrs = sorted(
            {
                networks[gn.network_id].cidr
                for gn in self.group.networks
                if gn.role is dm.NetworkRole.MGMT and gn.network_id in networks
            },
            key=lambda c: (c.version, str(c)),
        )
        if not cidrs:
            return None
        return {
            "port": EXPORTER_PORT,
            "cidrs": [str(c) for c in cidrs],
            "v6": any(c.version == 6 for c in cidrs),
        }

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
        options: list[str] = []
        if mode == "tcp" and svc.routes:
            request_rules = [
                "tcp-request inspect-delay 5s",
                "tcp-request content accept if { req.ssl_hello_type 1 }",
            ]
            routing_rules = self._sni_rules(svc, name)
        if mode == "http":
            options = self._http_options(svc, ports)
            request_rules = self._request_rules(svc, ports, allow)
            if any(p.port == 80 and not p.tls for p in ports):
                # First, before redirects and routes: the CA's request must reach the answer.
                request_rules.insert(0, _acme_rule())
                self.files[ACME_MAP_FILE] = _ACME_MAP_TEXT
            routing_rules = self._routing_rules(svc, name)
        return {
            "name": name,
            "mode": mode,
            "binds": [bind for p in ports for bind in self._binds(svc, p, name, mode)],
            "options": options,
            "connection_rules": connection_rules,
            "request_rules": request_rules,
            "routing_rules": routing_rules,
            "default_backend": (
                self._backend_name(svc.default_pool_id) if svc.default_pool_id else None
            ),
        }

    @staticmethod
    def _http_options(svc: dm.VirtualService, ports: list[dm.VsPort]) -> list[str]:
        """Frontend keywords of an HTTP service: compression, ``Alt-Svc`` (ADR 0116, 0117)."""
        options: list[str] = []
        if svc.compression is not None:
            options.append("compression algo " + " ".join(svc.compression.algorithms))
            options.append(
                "compression type "
                + " ".join(esc.media_type(t) for t in svc.compression.content_types)
            )
        for port in ports:
            if port.http3:
                options.append(f"http-response set-header alt-svc 'h3=\":{port.port}\"; ma=86400'")
        return options

    def _sni_rules(self, svc: dm.VirtualService, name: str) -> list[str]:
        """Route a passthrough ``tcp`` service by the SNI of the client hello (ADR 0115)."""
        exact: dict[str, str] = {}
        wild: dict[str, str] = {}
        for route in svc.routes:
            host = _need(route.match.host, "passthrough route host")
            target = self._backend_name(_need(route.pool_id, "passthrough route pool"))
            if _is_wild(host):
                wild[esc.wildcard_regex(host)] = target
            else:
                exact[esc.hostname(host)] = target
        rules: list[str] = []
        for converter, entries, suffix in (("map", exact, "sni"), ("map_reg", wild, "sni-wild")):
            if not entries:
                continue
            path = f"{BASE_DIR}/maps/{name}-{suffix}.map"
            self.files[f"haproxy/maps/{name}-{suffix}.map"] = _map_text(entries)
            lookup = f"req.ssl_sni,lower,{converter}({path})"
            rules.append(f"use_backend %[{lookup}] if {{ {lookup} -m found }}")
        return rules

    def _binds(self, svc: dm.VirtualService, port: dm.VsPort, name: str, mode: str) -> list[str]:
        bind = self._bind(svc, port, name, mode)
        if not port.http3:
            return [bind]
        # The same listener again over QUIC (UDP), with ALPN h3 instead of h2 and http/1.1.
        quic = bind.replace("alpn h2,http/1.1", "alpn h3")
        family = "quic6@" if svc.vip.version == 6 else "quic4@"
        address = f"[{svc.vip}]:{port.port}" if svc.vip.version == 6 else f"{svc.vip}:{port.port}"
        quic = quic.replace(
            f"bind {esc.bind_address(svc.vip, port.port)}", f"bind {family}{address}", 1
        )
        return [bind, quic]

    def _bind(self, svc: dm.VirtualService, port: dm.VsPort, name: str, mode: str) -> str:
        parts = [f"bind {esc.bind_address(svc.vip, port.port)}"]
        if port.tls:
            parts.append(f"ssl crt-list {CERT_DIR}/{name}.crtlist")
            if port.client_ca_id is not None:
                ca = self.certs[port.client_ca_id]
                ca_name = esc.ident(ca.name)
                parts.append(f"ca-file {CERT_DIR}/ca-{ca_name}.pem verify {port.client_verify}")
                if ca.crl_secret_ref:
                    parts.append(f"crl-file {CERT_DIR}/ca-{ca_name}.crl")
            if port.tls_profile is not None:
                minimum, ciphers, suites = TLS_PROFILES[port.tls_profile]
                parts.append(f"ssl-min-ver {minimum}")
                if ciphers is not None:
                    parts.append(f"ciphers {ciphers}")
                parts.append(f"ciphersuites {suites}")
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
            options = " [ocsp-update on]" if cert.ocsp_stapling else ""
            lines.append(f"{CERT_DIR}/{esc.ident(cert.name)}.pem{options} {snis}")
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
            lines += self._persistence_lines(pool.persistence)
        if pool.tunnel_timeout_s is not None:
            lines.append(f"timeout tunnel {pool.tunnel_timeout_s}s")
        if monitor is not None:
            lines += self._monitor_lines(monitor)
        servers = sorted(self._server(pool, m, monitor, mode) for m in pool.members)
        return {"name": self._backend_name(pool.id), "lines": lines, "servers": servers}

    @staticmethod
    def _persistence_lines(persistence: dm.Persistence) -> list[str]:
        """Backend lines for one persistence profile (ADR 0105)."""
        if isinstance(persistence, dm.PersistenceCookie):
            line = f"cookie {esc.cookie_name(persistence.name)} {persistence.mode}"
            if persistence.mode == "insert":
                line += " indirect nocache"
                if persistence.ttl_s is not None:
                    line += f" maxlife {persistence.ttl_s}s"
                if persistence.secure:
                    line += " secure"
                if persistence.http_only:
                    line += " httponly"
            return [line]
        if isinstance(persistence, dm.PersistenceSourceIp):
            table = f"type ipv6 size {persistence.table_size} expire {persistence.timeout_s}s"
            key = "src"
        else:
            table = (
                f"type string len 128 size {persistence.table_size} expire {persistence.timeout_s}s"
            )
            if isinstance(persistence, dm.PersistenceHeader):
                key = f"req.hdr({esc.header_name(persistence.header)})"
            elif persistence.source == "path":
                key = "path"
            else:
                key = f"url_param({esc.ident(_need(persistence.param, 'url parameter'))})"
        return [f"stick-table {table}", f"stick on {key}"]

    @staticmethod
    def _monitor_lines(monitor: dm.HealthMonitor) -> list[str]:
        lines: list[str] = []
        if monitor.preset is not None:
            lines.append(_TCP_PRESETS[monitor.preset])
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
            elif http.expect_body is not None:
                lines.append(f"http-check expect string {esc.match_text(http.expect_body)}")
            elif http.expect_header is not None:
                expected = http.expect_header
                line = f"http-check expect hdr name {esc.header_name(expected.name)}"
                if expected.value is not None:
                    line += f" value {esc.match_text(expected.value)}"
                lines.append(line)
        lines += [
            f"timeout check {monitor.timeout_s}s",
            f"default-server inter {monitor.interval_s}s fall {monitor.fall} rise {monitor.rise}",
        ]
        return lines

    def _server(
        self, pool: dm.Pool, member: dm.PoolMember, monitor: dm.HealthMonitor | None, mode: str
    ) -> str:
        address = member.address
        target = str(address) if address.version == 4 else f"ipv6@{address}"
        if member.port is not None:
            target += f":{member.port}"
        parts = [f"server {esc.ident(member.server_name)} {target}"]
        if isinstance(pool.persistence, dm.PersistenceCookie):
            parts.append(f"cookie {_cookie_value(pool, member)}")
        if monitor is not None:
            parts.append("check")
            if monitor.port is not None:
                parts.append(f"port {monitor.port}")
            if pool.tls:
                parts.append("check-ssl")
            if pool.send_proxy is not None:
                parts.append("check-send-proxy")
            if pool.passive_check is not None:
                passive = pool.passive_check
                parts.append(
                    f"observe {passive.observe} error-limit {passive.error_limit} "
                    f"on-error {passive.on_error}"
                )
            if pool.slow_start_s is not None:
                parts.append(f"slowstart {pool.slow_start_s}s")
        if pool.tls:
            parts.append(
                f"ssl verify required ca-file {CA_BUNDLE}" if pool.tls_verify else "ssl verify none"
            )
            if pool.client_certificate_id is not None:
                cert = self.certs[pool.client_certificate_id]
                parts.append(f"crt {CERT_DIR}/{esc.ident(cert.name)}.pem")
            if mode == "http":
                parts.append("sni req.hdr(host),field(1,:),lower")
            if pool.backend_protocol == "h2":
                parts.append("alpn h2")
        if pool.backend_protocol == "h2c":
            parts.append("proto h2")
        if pool.send_proxy is not None:
            parts.append("send-proxy" if pool.send_proxy == "v1" else "send-proxy-v2")
        parts.append(f"weight {member.weight}")
        if member.maxconn is not None:
            parts.append(f"maxconn {member.maxconn}")
        if member.backup:
            parts.append("backup")
        if member.admin_state is not dm.AdminState.ENABLED:
            parts.append("disabled")  # drain and disabled both start out of rotation
        return " ".join(parts)
