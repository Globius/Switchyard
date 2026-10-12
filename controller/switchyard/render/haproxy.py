"""Render an SE group's desired state to HAProxy configuration files.

:func:`render_haproxy` is a pure function: the same :class:`~switchyard.domain.snapshot.Snapshot`
always yields byte-identical files. Everything is sorted (frontends by service name, backends by
pool name, servers by server name, map and list lines by key) and nothing reads the clock, the
environment or the network.

Files produced (paths are relative to ``/etc/switchyard/`` on the SE, the only directory the agent
and the Data Plane API write to):

================================= =================================================================
``haproxy/haproxy.cfg``           global, defaults, one frontend per service, one backend per pool
``certs/<vs>.crtlist``            certificates and their SNI filters for a service's TLS ports
``haproxy/maps/<vs>-host.map``    exact host -> backend, for host-only routes
``haproxy/maps/<vs>-wild.map``    ``*.`` wildcard host regex -> backend, for host-only routes
``haproxy/lists/<policy>.cidr``   source networks of an allow-list policy set
``haproxy/lists/<policy>-*.page`` bodies of fixed responses and error pages
================================= =================================================================

Certificate and key material is *not* rendered: ``crt-list`` entries point at
``/etc/switchyard/certs/<certificate-name>.pem``, which the agent writes from OpenBao.

``haproxy/haproxy.cfg`` carries ``# switchyard-render: sha256:<hex>``, a hash of every file. Drift
detection compares it with what an SE reports. The crt-list lives beside the certificates because
the Data Plane API stores both in one directory, and object names contain no dots because the API
rewrites dots in file names.

Request handling order inside an HTTP frontend, which the output makes explicit (``docs/render.md``,
ADR 0100):

1. connection filter (allow list), client-IP restoration, header hygiene;
2. HTTP to HTTPS redirects, then policy-set redirects, then redirect routes;
3. fixed responses (``respond`` policy sets);
4. request rewrites (``rewrite`` policy sets, request phase);
5. conditional pool routes (a route with more than a host), in route ``order``;
6. host-only pool routes: exact host first, then single-label wildcard hosts;
7. the default pool, if any; response rewrites run on the way out.
"""

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from jinja2 import Environment, PackageLoader, StrictUndefined

from switchyard.domain import models as dm
from switchyard.domain.snapshot import (
    Snapshot,
    ensure_valid,
    protection_set,
    rate_limit_rules,
)
from switchyard.render import captcha, waf
from switchyard.render import escape as esc
from switchyard.render.auth import AuthInstance, auth_instances
from switchyard.render.escape import RenderError
from switchyard.render.tokens import key_file

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
PEERS_SECTION = "sy_peers"

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


def render_haproxy(
    snapshot: Snapshot, se_group_id: UUID, broker_url: str | None = None
) -> RenderResult:
    """Render the HAProxy files of one SE group.

    The snapshot is reduced to what the group references and validated first, so a render never
    starts from state the domain rules reject.

    Args:
        snapshot: Desired state; may contain other SE groups and tenants.
        se_group_id: The group to render.
        broker_url: Public base URL of the login broker (``SWITCHYARD_AUTH_BROKER_URL``); only
            needed by access rules with ``min_factors``, which redirect to the login gate
            (ADR 0195).

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
    return _Renderer(view, broker_url).render()


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


#: Sample fetches behind the header variables (the fixed list; users never write expressions).
_VARIABLES = {
    dm.HeaderVariable.CLIENT_IP: "src",
    dm.HeaderVariable.CLIENT_PORT: "src_port",
    dm.HeaderVariable.HOST: "var(txn.host)",
    dm.HeaderVariable.VIP: "dst",
    dm.HeaderVariable.VIP_PORT: "dst_port",
    dm.HeaderVariable.METHOD: "method",
    dm.HeaderVariable.TLS_VERSION: "ssl_fc_protocol",
    dm.HeaderVariable.TLS_CIPHER: "ssl_fc_cipher",
    dm.HeaderVariable.TLS_SNI: "ssl_fc_sni",
    dm.HeaderVariable.REQUEST_ID: "uuid",
}
#: Back-reference to the first regex group in a ``replace-path`` replacement (single-quoted, so
#: HAProxy passes the backslash through unchanged).
_GROUP_1 = "\\1"
#: Header comparisons as ACL match flags.
_HEADER_FLAGS = {
    dm.HeaderOp.EQUALS: "str",
    dm.HeaderOp.PREFIX: "beg",
    dm.HeaderOp.SUFFIX: "end",
    dm.HeaderOp.CONTAINS: "sub",
}
#: The request came back from the authentication proxy on the loopback bind (ADR 0130).
LOOP_ON = "{ var(txn.sy_loop) -m bool }"
LOOP_OFF = "!{ var(txn.sy_loop) -m bool }"
#: Headers the authentication proxy sets for the engine; never sent to the origin (ADR 0132).
PROXY_HEADERS = (
    "X-Forwarded-User",
    "X-Forwarded-Email",
    "X-Forwarded-Groups",
    "X-Forwarded-Preferred-Username",
    "X-Forwarded-Access-Token",
)
#: Tells the second hop that the first saw a verified client certificate (ADR 0138).
DEVICE_HEADER = "X-Sy-Device"
#: Carries the access decision from the first hop to the loopback hop.
ACCESS_HEADER = "X-Sy-Access"
#: Frontend rules by stage, filled while the policy sets of a service are rendered.
_Stages = tuple[list[str], list[str], list[str], list[str]]


def _claim_forms(values: tuple[str, ...]) -> list[str]:
    """Spellings to match for claim values; ``true`` and ``false`` also match JSON booleans.

    HAProxy renders a JSON boolean claim as ``1`` or ``0``, a string claim as written.

    Example:
        >>> _claim_forms(("true", "gold"))
        ['1', 'gold', 'true']
    """
    forms = set(values)
    for value in values:
        if value in ("true", "false"):
            forms.add("1" if value == "true" else "0")
    return sorted(forms)


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


_CHALLENGE_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Checking your browser</title></head>
<body style="font-family:sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem">
<h1>Checking your browser</h1>
<p>This takes a moment. JavaScript must be enabled.</p>
<noscript><p>Please enable JavaScript to continue.</p></noscript>
<script>
(async () => {
  const ts = "%[date]", ip = "%[src]", salt = "@SALT@", zeros = "0".repeat(@ZEROS@);
  const enc = new TextEncoder();
  for (let n = 0; ; n++) {
    const v = ts + "-" + n;
    const d = await crypto.subtle.digest("SHA-256", enc.encode(v + "|" + ip + "|" + salt));
    const h = Array.from(new Uint8Array(d), b => b.toString(16).padStart(2, "0")).join("");
    if (h.startsWith(zeros)) {
      const secure = location.protocol === "https:" ? "; Secure" : "";
      document.cookie = "sy_chal=" + v + "; Path=/; Max-Age=@VALID@; SameSite=Lax" + secure;
      location.reload();
      return;
    }
  }
})();
</script>
</body></html>
"""


def _challenge_page(difficulty: int, valid_for_s: int, salt: str) -> str:
    """The challenge page; ``%[date]`` and ``%[src]`` are filled in by HAProxy per request."""
    return (
        _CHALLENGE_PAGE.replace("@SALT@", salt)
        .replace("@ZEROS@", str(int(difficulty)))
        .replace("@VALID@", str(int(valid_for_s)))
    )


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
    def __init__(self, snapshot: Snapshot, broker_url: str | None = None) -> None:
        self.snap = snapshot
        self.broker_url = broker_url
        self.group = snapshot.se_groups[0]
        self.pools = {p.id: p for p in snapshot.pools}
        self.monitors = {m.id: m for m in snapshot.health_monitors}
        self.certs = {c.id: c for c in snapshot.certificates}
        self.policies = {p.id: p for p in snapshot.policy_sets}
        self.files: dict[str, str] = {}
        self.warnings: list[str] = []
        self.notices: list[RenderWarning] = []
        self.pool_modes: dict[UUID, str] = {}
        self.uses_waf = False
        self.lua_loads: list[str] = []
        self.limit_backends: list[dict[str, Any]] = []
        self.instances = {i.service.id: i for i in auth_instances(snapshot, self.group.id)}
        self.guard = ""
        self.stick_counters = 0

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
        backends += self.limit_backends
        backends.sort(key=lambda b: b["name"])
        template = _ENV.get_template("haproxy.cfg.j2")

        def cfg(fingerprint: str) -> str:
            return template.render(
                group=esc.ident(self.group.name),
                maxconn=self.group.maxconn,
                frontends=frontends,
                backends=backends,
                exporter=self._exporter(),
                stick_counters=self.stick_counters if self.stick_counters > 3 else None,
                fingerprint=fingerprint,
                peers=self._peers(),
                caches=self._caches(),
                lua_loads=self.lua_loads,
                waf_backend=waf.backend_lines() if self.uses_waf else [],
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

    def _peers(self) -> dict[str, Any] | None:
        """The ``peers`` section that syncs persistence tables between the group's engines."""
        sync = self.group.peers
        if sync is None:
            return None
        return {
            "name": PEERS_SECTION,
            "members": [
                {"name": esc.ident(m.name), "target": esc.bind_address(m.address, sync.port)}
                for m in sorted(sync.members, key=lambda m: m.name)
            ],
        }

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
        ids = [pid for r in svc.routes for pid in r.pool_ids]
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
        inst = self.instances.get(svc.id) if mode == "http" else None
        self.guard = LOOP_OFF if inst is not None else ""
        connection_rules: list[str] = []
        if allow is not None:
            self.files[f"haproxy/lists/{esc.ident(allow.name)}.cidr"] = self._allow_list_text(allow)
            if svc.enforce_allow_list:
                lst = self._list_path(allow)
                # the loopback hop of a login comes from the proxy, not from an allowed client
                exempt = " !{ dst 127.0.0.1 }" if inst is not None else ""
                connection_rules.append(
                    f"tcp-request session reject if !{{ src -f {lst} }}{exempt}"
                )
        deny = _allow_list(self.policies[svc.deny_list_id]) if svc.deny_list_id else None
        if deny is not None:
            self.files[f"haproxy/lists/{esc.ident(deny.name)}.cidr"] = self._allow_list_text(deny)
            if mode != "http":
                connection_rules.append(
                    f"tcp-request session reject if {{ src -f {self._list_path(deny)} }}"
                )
        request_rules: list[str] = []
        tarpit_s: int | None = None
        response_rules: list[str] = []
        error_rules: list[str] = []
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
            protection = protection_set(svc, self.policies)
            if protection is not None:
                connection_rules = self._connection_protection(svc, protection) + connection_rules
                options += self._timeout_options(protection)
            request_rules = self._request_rules(svc, ports, allow, deny, inst)
            if protection is not None:
                request_rules += self._bot_rules(protection)
                request_rules += self._challenge_rules(svc, protection)
                request_rules += self._captcha_rules(svc, protection)
            tarpit_s = self._tarpit(svc)
            response_rules = self._policy_stages(svc)[3]
            error_rules = self._error_lines(svc)
            if any(p.port == 80 and not p.tls for p in ports):
                # First, before redirects and routes: the CA's request must reach the answer.
                request_rules.insert(0, _acme_rule())
                self.files[ACME_MAP_FILE] = _ACME_MAP_TEXT
            routing_rules = self._routing_rules(svc, name)
            policy = waf.waf_policy(svc, self.policies)
            if policy is not None:
                waf_options, waf_rules = waf.frontend_lines(svc, policy)
                options += waf_options
                request_rules += waf_rules
                self.files[waf.spoe_path(svc)] = waf.render_spoe(svc, policy)
                self.uses_waf = True
            if svc.cache is not None:
                cache = self._cache_name(svc)
                request_rules.append(f"http-request cache-use {cache}")
                response_rules.append(f"http-response cache-store {cache}")
            if inst is not None:
                routing_rules = self._auth_routing(inst) + routing_rules
                self.limit_backends.append(
                    {
                        "name": inst.backend,
                        "lines": ["mode http"],
                        "servers": [f"server auth 127.0.0.1:{inst.auth_port}"],
                    }
                )
        binds = [bind for p in ports for bind in self._binds(svc, p, name, mode)]
        if inst is not None:
            binds.append(f"bind 127.0.0.1:{inst.loop_port}")
        return {
            "name": name,
            "mode": mode,
            "binds": binds,
            "options": options,
            "connection_rules": connection_rules,
            "request_rules": request_rules,
            "response_rules": response_rules,
            "error_rules": error_rules,
            "tarpit_s": tarpit_s,
            "routing_rules": routing_rules,
            "default_backend": (
                self._backend_name(svc.default_pool_id) if svc.default_pool_id else None
            ),
        }

    @staticmethod
    @staticmethod
    def _cache_name(svc: dm.VirtualService) -> str:
        return f"sy_cache_{esc.ident(svc.name)}"

    def _caches(self) -> list[dict[str, Any]]:
        """The ``cache`` sections of the enabled HTTP services (ADR 0117)."""
        out = []
        for svc in sorted(self.snap.virtual_services, key=lambda s: s.name):
            if svc.enabled and svc.cache is not None and svc.protocol is dm.Protocol.HTTP:
                out.append(
                    {
                        "name": self._cache_name(svc),
                        "size_mb": svc.cache.size_mb,
                        "max_object": svc.cache.max_object_kb * 1024,
                        "max_age": svc.cache.max_age_s,
                    }
                )
        return out

    def _connection_protection(
        self, svc: dm.VirtualService, protection: dm.ProtectionPolicySet
    ) -> list[str]:
        """Per-source connection limits, tracked at connection time (ADR 0170)."""
        rules: list[str] = []
        if protection.exempt_cidrs:
            rules.append(f"tcp-request connection accept if {self._exempt(protection)}")
        if not protection.tracks_connections:
            return rules
        slot = len(rate_limit_rules(svc, self.policies))
        table = f"pt_{esc.ident(svc.name)}"
        period = protection.connection_rate_period_s
        self.limit_backends.append(
            {
                "name": table,
                "lines": [
                    f"stick-table type ipv6 size {protection.table_size} expire 60s "
                    f"store conn_cur,conn_rate({period}s)"
                ],
                "servers": [],
            }
        )
        self.stick_counters = max(self.stick_counters, slot + 1)
        rules.append(f"tcp-request connection track-sc{slot} src table {table}")
        if protection.max_connections_per_source:
            limit = protection.max_connections_per_source
            rules.append(
                f"tcp-request connection reject if {{ sc_conn_cur({slot},{table}) gt {limit} }}"
            )
        if protection.max_new_connections_per_source:
            limit = protection.max_new_connections_per_source
            rules.append(
                f"tcp-request connection reject if {{ sc_conn_rate({slot},{table}) gt {limit} }}"
            )
        return rules

    def _challenge_rules(
        self, svc: dm.VirtualService, protection: dm.ProtectionPolicySet
    ) -> list[str]:
        """The JavaScript proof-of-work gate (ADR 0171).

        The cookie ``sy_chal`` holds ``<epoch>-<counter>``. A request passes when
        ``sha256("<cookie>|<client address>|<salt>")`` starts with the required zeros and the epoch
        is at most ``valid_for_s`` old. The salt is public and per service, so a solved cookie is
        good for one client address and one service only.
        """
        gate = protection.challenge
        if gate is None:
            return []
        name = esc.ident(svc.name)
        salt = hashlib.sha256(f"{svc.id}/{protection.id}".encode()).hexdigest()[:16]
        self.files[f"haproxy/lists/challenge-{name}.html"] = _challenge_page(
            gate.difficulty, gate.valid_for_s, salt
        )
        skips = []
        if protection.exempt_cidrs:
            skips.append(f"!{self._exempt(protection)}")
        if gate.skip_paths:
            paths = " ".join(esc.arg(p) for p in sorted(gate.skip_paths))
            skips.append(f"!{{ path_beg {paths} }}")
        if gate.skip_user_agents:
            words = " ".join(esc.user_agent_token(t) for t in sorted(gate.skip_user_agents))
            skips.append(f"!{{ req.hdr(user-agent),lower -m sub {words} }}")
        unsolved = " ".join([*skips, "!{ var(txn.sy_pass) -m bool }"])
        has = "if { var(txn.sy_cv) -m found }"
        zeros = "0" * gate.difficulty
        page = f"{BASE_DIR}/lists/challenge-{name}.html"
        return [
            "http-request set-var(txn.sy_cv) req.cook(sy_chal) if { req.cook(sy_chal) -m found }",
            f"http-request set-var(txn.sy_ts) var(txn.sy_cv),field(1,-),add(0) {has}",
            f'http-request set-var-fmt(txn.sy_in) "%[var(txn.sy_cv)]|%[src]|{salt}" {has}',
            f"http-request set-var(txn.sy_hash) var(txn.sy_in),sha2(256),hex {has}",
            f"http-request set-var(txn.sy_age) date,sub(txn.sy_ts) {has}",
            "http-request set-var(txn.sy_pass) bool(true)"
            f" if {{ var(txn.sy_hash) -m beg {zeros} }} {{ var(txn.sy_age) -m int ge 0 }}"
            f" {{ var(txn.sy_age) -m int le {gate.valid_for_s} }}",
            "http-request return status 200 content-type text/html hdr cache-control no-store"
            f" lf-file {page} if {{ method GET HEAD }} {unsolved}",
            f"http-request deny deny_status 403 if {unsolved}",
        ]

    def _captcha_rules(
        self, svc: dm.VirtualService, protection: dm.ProtectionPolicySet
    ) -> list[str]:
        """The CAPTCHA gate: Lua check, Lua verify service, page for the unverified (ADR 0172)."""
        gate = protection.captcha
        if gate is None:
            return []
        name = esc.ident(svc.name)
        ident = captcha.lua_id(svc)
        self.files[f"haproxy/lists/captcha-{name}.lua"] = captcha.render_lua(svc, gate)
        self.files[f"haproxy/lists/captcha-{name}.html"] = captcha.render_page(gate)
        self.lua_loads.append(f"{BASE_DIR}/lists/captcha-{name}.lua")
        skips = []
        if protection.exempt_cidrs:
            skips.append(f"!{self._exempt(protection)}")
        if gate.skip_paths:
            paths = " ".join(esc.arg(p) for p in sorted(gate.skip_paths))
            skips.append(f"!{{ path_beg {paths} }}")
        if gate.skip_user_agents:
            words = " ".join(esc.user_agent_token(t) for t in sorted(gate.skip_user_agents))
            skips.append(f"!{{ req.hdr(user-agent),lower -m sub {words} }}")
        unsolved = " ".join([*skips, "!{ var(txn.sy_pass) -m bool }"])
        return [
            f"http-request lua.sy_cap_check_{ident}",
            f"http-request use-service lua.sy_cap_{ident} if {{ path {captcha.SUBMIT_PATH} }}"
            " { method POST }",
            "http-request return status 200 content-type text/html hdr cache-control no-store"
            f" lf-file {BASE_DIR}/lists/captcha-{name}.html if {{ method GET HEAD }} {unsolved}",
            f"http-request deny deny_status 403 if {unsolved}",
        ]

    @staticmethod
    def _exempt(protection: dm.ProtectionPolicySet) -> str:
        nets = sorted(protection.exempt_cidrs, key=lambda n: (n.version, int(n.network_address)))
        return "{ src " + " ".join(str(n) for n in nets) + " }"

    @staticmethod
    def _timeout_options(protection: dm.ProtectionPolicySet) -> list[str]:
        options = []
        if protection.header_timeout_s is not None:
            options.append(f"timeout http-request {protection.header_timeout_s}s")
        if protection.client_timeout_s is not None:
            options.append(f"timeout client {protection.client_timeout_s}s")
        return options

    def _bot_rules(self, protection: dm.ProtectionPolicySet) -> list[str]:
        """User-agent rules: empty agents, known scanners, and the tenant's own list."""
        action = (
            "http-request deny deny_status 403"
            if protection.bot_action == "deny"
            else "http-request silent-drop"
        )
        exempt = f" !{self._exempt(protection)}" if protection.exempt_cidrs else ""
        rules: list[str] = []
        if protection.block_empty_user_agent:
            rules.append(f"{action} if !{{ req.hdr(user-agent) -m found }}{exempt}")
            rules.append(f"{action} if {{ req.hdr(user-agent) -m len 0 }}{exempt}")
        tokens = list(dm.KNOWN_SCANNERS) if protection.block_scanners else []
        tokens += sorted(set(protection.deny_user_agents) - set(tokens))
        if tokens:
            words = " ".join(esc.user_agent_token(t) for t in tokens)
            rules.append(f"{action} if {{ req.hdr(user-agent),lower -m sub {words} }}{exempt}")
        return rules

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
        quic = bind.replace(",acme-tls/1", "").replace("alpn h2,http/1.1", "alpn h3")
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
                # acme-tls/1 lets the controller answer TLS-ALPN-01 challenges (ADR 0082); no
                # browser or API client offers it, so it changes nothing for them.
                parts.append(
                    "alpn h2,http/1.1,acme-tls/1" if port.http2 else "alpn http/1.1,acme-tls/1"
                )
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

    def _limit_rules(self, svc: dm.VirtualService) -> list[str]:
        """Track and enforce the service's rate-limit rules; each rule gets a counter and a table.

        The table is a dummy backend that only holds the stick table; the counter slot
        (``track-sc<n>``) is the rule's position in the service, so rules never share state.
        """
        rules: list[str] = []
        for slot, (policy, index, rule) in enumerate(rate_limit_rules(svc, self.policies)):
            table = f"rl_{esc.ident(svc.name)}_{esc.ident(policy.name)}_{index}"
            esc.ident(table)
            if rule.key == "header":
                key = f"req.hdr({esc.header_name(_need(rule.header_name, 'header name'))})"
                table_type = "string len 64"
            else:
                key = "src"
                table_type = "ipv6"
            if rule.max_requests is not None:
                store = f"http_req_rate({rule.period_s}s)"
                check = f"{{ sc_http_req_rate({slot},{table}) gt {rule.max_requests} }}"
                expire = max(rule.period_s, 60)
                retry = f" hdr Retry-After {rule.period_s}"
            else:
                store = "conn_cur"
                check = (
                    f"{{ sc_conn_cur({slot},{table}) gt {_need(rule.max_concurrent, 'limit')} }}"
                )
                expire = 60
                retry = ""
            self.limit_backends.append(
                {
                    "name": table,
                    "lines": [
                        f"stick-table type {table_type} size {rule.table_size} "
                        f"expire {expire}s store {store}"
                    ],
                    "servers": [],
                }
            )
            self.stick_counters = max(self.stick_counters, slot + 1)
            cond = self._cond(rule.match)
            track = f"http-request track-sc{slot} {key} table {table}"
            rules.append(track + (f" if {cond}" if cond else ""))
            if rule.action == "deny":
                rules.append(f"http-request return status {rule.deny_status}{retry} if {check}")
            elif rule.action == "tarpit":
                rules.append(f"http-request tarpit deny_status 429 if {check}")
            else:
                rules.append(f"http-request silent-drop if {check}")
        return rules

    def _tarpit(self, svc: dm.VirtualService) -> int | None:
        delays = {
            r.tarpit_s for _, _, r in rate_limit_rules(svc, self.policies) if r.action == "tarpit"
        }
        return min(delays) if delays else None

    def _policy_stages(self, svc: dm.VirtualService) -> _Stages:
        """Rules of the attached policy sets: redirects, responses, request and response rewrites.

        Sets apply in attachment order within their kind; the kinds run in the stage order of
        ``docs/render.md``, not in attachment order across kinds (ADR 0100).
        """
        redirects: list[str] = []
        responses: list[str] = []
        rewrites: list[str] = []
        outgoing: list[str] = []
        flags: list[str] = []
        for pid in svc.policy_set_ids:
            policy = self.policies[pid]
            if isinstance(policy, dm.RedirectPolicySet):
                for rule in policy.rules:
                    redirects.append(self._redirect_line(rule.match, rule.redirect))
            elif isinstance(policy, dm.RespondPolicySet):
                for index, response in enumerate(policy.rules, start=1):
                    responses.append(self._response_line(policy, index, response))
            elif isinstance(policy, dm.RewritePolicySet):
                for rewrite in policy.rules:
                    cond = self._cond(rewrite.match)
                    if rewrite.phase == "request":
                        rewrites += self._rewrite_lines(rewrite, cond)
                        continue
                    if not cond:
                        outgoing += self._rewrite_lines(rewrite, cond)
                        continue
                    # The request attributes are gone by the time a response rule runs, so the
                    # match is evaluated with the request and remembered in a transaction flag.
                    flag = f"txn.sy_m{len(flags) + 1}"
                    flags.append(flag)
                    rewrites.append(f"http-request set-var({flag}) bool(true) if {cond}")
                    outgoing += self._rewrite_lines(rewrite, f"{{ var({flag}) -m bool }}")
        return redirects, responses, rewrites, outgoing

    def _cond(self, match: dm.RouteMatch) -> str:
        """The conditions of a match plus the stage guard (not on the loopback hop of a login)."""
        return " ".join([*self._conditions(match), *([self.guard] if self.guard else [])])

    def _redirect_line(self, match: dm.RouteMatch, redirect: dm.RedirectAction) -> str:
        cond = self._cond(match)
        location = esc.location(redirect.location)
        return f"http-request redirect location {location} code {redirect.code} if {cond}"

    def _page(self, policy: dm.TenantOwned, tag: str, body: str) -> str:
        """Store a response body as a general-storage file (beside the lists); return its path."""
        name = f"{esc.ident(policy.name)}-{tag}.page"
        self.files[f"haproxy/lists/{name}"] = body if body.endswith("\n") else body + "\n"
        return f"{BASE_DIR}/lists/{name}"

    def _response_line(self, policy: dm.RespondPolicySet, index: int, rule: dm.ResponseRule) -> str:
        parts = [f"http-request return status {rule.status}"]
        if rule.body is not None:
            parts.append(f"content-type {_need(rule.content_type, 'content type')}")
            parts.append(f"file {self._page(policy, f'r{index}', rule.body)}")
        if rule.retry_after_s is not None:
            parts.append(f"hdr Retry-After {rule.retry_after_s}")
        cond = self._cond(rule.match)
        return " ".join(parts) + (f" if {cond}" if cond else "")

    def _error_lines(self, svc: dm.VirtualService) -> list[str]:
        lines: dict[int, str] = {}
        for pid in svc.policy_set_ids:
            policy = self.policies[pid]
            if isinstance(policy, dm.ErrorPagesPolicySet):
                for page in policy.pages:
                    path = self._page(policy, f"e{page.status}", page.body)
                    lines[page.status] = (
                        f"http-error status {page.status} content-type {page.content_type} "
                        f"file {path}"
                    )
        return [lines[status] for status in sorted(lines)]

    def _rewrite_lines(self, rule: dm.RewriteRule, cond: str) -> list[str]:
        phase = "http-response" if rule.phase == "response" else "http-request"
        suffix = f" if {cond}" if cond else ""
        action = rule.action
        if isinstance(action, dm.RemoveHeader):
            return [f"{phase} del-header {esc.header_name(action.name)}{suffix}"]
        if isinstance(action, dm.ReplacePrefix):
            return self._replace_prefix(action, cond)
        verb = "set-header" if isinstance(action, dm.SetHeader) else "add-header"
        if action.variable is not None:
            value = f'"%[{_VARIABLES[action.variable]}]"'
        else:
            value = esc.quoted(_need(action.value, "header value"))
        return [f"{phase} {verb} {esc.header_name(action.name)} {value}{suffix}"]

    @staticmethod
    def _replace_prefix(action: dm.ReplacePrefix, cond: str) -> list[str]:
        """``replace-path`` on path-segment boundaries; ``/`` as target strips the prefix.

        The regex and the replacement are single-quoted (nothing inside is expanded) and are
        built from validated path characters, with ``.`` and ``+`` written as classes.
        """
        source = esc.path_regex(action.from_prefix)
        target = esc.path_text(action.to_prefix)
        suffix = f" if {cond}" if cond else ""
        if action.to_prefix == "/":
            exact = f"{{ path {esc.arg(action.from_prefix)} }}"
            guard = f" if {cond} {exact}" if cond else f" if {exact}"
            return [
                f"http-request set-path /{guard}",
                f"http-request replace-path '^{source}(/.*)$' '{_GROUP_1}'{suffix}",
            ]
        return [f"http-request replace-path '^{source}(/.*)?$' '{target}{_GROUP_1}'{suffix}"]

    def _jwt_rules(self, svc: dm.VirtualService) -> list[str]:
        """Validate bearer tokens (stage 4b, ADR 0135); the first rule that matches decides."""
        rules: list[str] = []
        tok = "var(txn.sy_jtok)"
        count = 0
        for pid in svc.policy_set_ids:
            policy = self.policies[pid]
            if not isinstance(policy, dm.JwtPolicySet):
                continue
            for index, rule in enumerate(policy.rules, start=1):
                if not rules:
                    rules += [
                        f"http-request set-var(txn.sy_jtok) http_auth_bearer{self._if()}",
                        f"http-request set-var(txn.sy_jnow) date{self._if()}",
                    ]
                count += 1
                flag = f"txn.sy_j{count}"
                match = self._conditions(rule.match)
                fresh = "!{ var(txn.sy_jdone) -m found }"
                base = " ".join([*match, *([self.guard] if self.guard else []), fresh])
                claims = [
                    f"{{ {tok},jwt_payload_query('$.iss') -m str {esc.issuer(rule.issuer)} }}"
                ]
                if rule.audiences:
                    alt = "|".join(sorted(esc.audience_regex(a) for a in rule.audiences))
                    claims.append(
                        f"{{ {tok},jwt_payload_query('$.aud') -m reg '(^|\")({alt})(\"|$)' }}"
                    )
                for scope in sorted(rule.required_scopes):
                    claims.append(
                        f"{{ {tok},jwt_payload_query('$.scope') -m reg "
                        f"'(^| ){esc.group_regex(scope)}( |$)' }}"
                    )
                claims.append(
                    f"{{ {tok},jwt_payload_query('$.exp','int'),sub(txn.sy_jnow),"
                    f"add({rule.leeway_s}) -m int 1: }}"
                )
                if rule.subject_header:
                    name = esc.header_name(rule.subject_header)
                    rules.append(f"http-request del-header {name}{self._if()}")
                for key_no, key in enumerate(rule.keys, start=1):
                    path = key_file(policy.name, index, key_no)
                    conds = [base, f"!{{ var({flag}) -m found }}"]
                    conds.append(f"{{ {tok},jwt_verify({key.alg},{esc.arg(path)}) -m int 1 }}")
                    if key.kid:
                        conds.append(
                            f"{{ {tok},jwt_header_query('$.kid') -m str {esc.arg(key.kid)} }}"
                        )
                    conds += claims
                    rules.append(f"http-request set-var({flag}) int(1) if {' '.join(conds)}")
                rules.append(
                    f"http-request return status 401 hdr WWW-Authenticate Bearer "
                    f"if {base} !{{ var({flag}) -m found }}"
                )
                if rule.subject_header:
                    name = esc.header_name(rule.subject_header)
                    rules.append(
                        f"http-request set-var(txn.sy_jsub{count}) "
                        f"{tok},jwt_payload_query('$.sub') if {{ var({flag}) -m found }}"
                    )
                    rules.append(
                        f'http-request set-header {name} "%[var(txn.sy_jsub{count})]" '
                        f"if {{ var({flag}) -m found }}"
                    )
                rules.append(f"http-request set-var(txn.sy_jdone) int(1) if {base}")
        return rules

    def _cert_rules(self, svc: dm.VirtualService) -> list[str]:
        """Use the TLS client certificate as the identity (stage 4b, ADR 0136)."""
        rules: list[str] = []
        policy = next(
            (
                p
                for pid in svc.policy_set_ids
                if isinstance(p := self.policies[pid], dm.ClientCertPolicySet)
            ),
            None,
        )
        if policy is None:
            return rules
        name = esc.ident(policy.name)
        for header in (policy.user_header, policy.groups_header):
            if header:
                rules.append(f"http-request del-header {esc.header_name(header)}{self._if()}")
        used = "{ ssl_c_used }"
        guard = [self.guard] if self.guard else []
        by_kind: dict[str, dict[str, str]] = {"cn": {}, "sha1": {}}
        for ident in policy.identities:
            by_kind[ident.by][esc.cert_key(ident.value, ident.by)] = (
                esc.cert_label(ident.user)
                + "|"
                + ",".join(esc.group_label(g) for g in ident.groups)
            )
        for kind, fetch in (("sha1", "ssl_c_sha1,hex,lower"), ("cn", "ssl_c_s_dn(cn)")):
            if by_kind[kind]:
                path = f"haproxy/maps/{name}-cert{kind}.map"
                self.files[path] = _map_text(by_kind[kind])
                unset = "!{ var(txn.sy_cid) -m found }"
                cond = " ".join([used, unset, *guard])
                lookup = f"{fetch},map({BASE_DIR}/maps/{name}-cert{kind}.map)"
                rules.append(f"http-request set-var(txn.sy_cid) {lookup} if {cond}")
        if any(by_kind.values()):
            rules.append(
                "http-request set-var(txn.sy_cuser) var(txn.sy_cid),field(1,'|') "
                "if { var(txn.sy_cid) -m found }"
            )
            rules.append(
                "http-request set-var(txn.sy_cgrp) var(txn.sy_cid),field(2,'|') "
                "if { var(txn.sy_cid) -m found }"
            )
        for rule in policy.rules:
            base = " ".join(
                [*self._conditions(rule.match), *guard, "!{ var(txn.sy_cdone) -m found }"]
            )
            if rule.mode == "valid":
                test = f"!{used}"
            elif rule.mode == "identity":
                test = "!{ var(txn.sy_cid) -m found }"
            else:
                regex = "|".join(sorted(esc.group_regex(g) for g in rule.groups))
                test = f"!{{ var(txn.sy_cgrp) -m reg '(^|,)({regex})(,|$)' }}"
            rules.append(f"http-request return status 403 if {base} {test}")
            rules.append(f"http-request set-var(txn.sy_cdone) int(1) if {base}")
        if policy.user_header:
            rules.append(
                f"http-request set-header {esc.header_name(policy.user_header)} "
                f'"%[var(txn.sy_cuser)]" if {{ var(txn.sy_cuser) -m found }}{self._g()}'
            )
        if policy.groups_header:
            rules.append(
                f"http-request set-header {esc.header_name(policy.groups_header)} "
                f'"%[var(txn.sy_cgrp)]" if {{ var(txn.sy_cgrp) -m found }}{self._g()}'
            )
        return rules

    def _if(self) -> str:
        """`` if <guard>`` for lines that need only the stage guard (empty without one)."""
        return f" if {self.guard}" if self.guard else ""

    def _g(self) -> str:
        """The stage guard as a trailing condition block (empty when there is none)."""
        return f" {self.guard}" if self.guard else ""

    @staticmethod
    def _loop_entry(inst: AuthInstance) -> list[str]:
        """Recognise the loopback hop of a login and recover the real client address on it."""
        return [
            "http-request set-var(txn.sy_loop) bool(true) "
            f"if {{ dst 127.0.0.1 }} {{ dst_port {inst.loop_port} }}",
            f"http-request set-src hdr(X-Forwarded-For) if {LOOP_ON}",
        ]

    @staticmethod
    def _identity_names(inst: AuthInstance) -> list[str]:
        """Headers a client may never send: ours, and the proxy's own (ADR 0132)."""
        ours = inst.policy.identity_headers
        names = [n for n in (ours.user, ours.email, ours.groups, ours.token) if n]
        return [*names, *PROXY_HEADERS, ACCESS_HEADER, DEVICE_HEADER]

    def _auth_hygiene(self, inst: AuthInstance) -> list[str]:
        """Drop client-supplied identity headers; only the loopback hop may carry them."""
        return [
            f"http-request del-header {esc.header_name(name)} if {LOOP_OFF}"
            for name in self._identity_names(inst)
        ]

    def _access_rules(self, inst: AuthInstance) -> list[str]:
        """Decide what each request needs, route it through the proxy, enforce it on return.

        On the first hop ``txn.sy_ad`` becomes 1 (public) or ``k + 2`` (rule *k* needs a login; the
        default needs ``len(rules) + 2``). The value travels to the loopback hop in a header that
        client requests cannot set (it is removed in the hygiene stage), where the groups and
        e-mail checks run on the identity the proxy vouched for.
        """
        policy = inst.policy
        rules: list[str] = []
        fresh = "!{ var(txn.sy_ad) -m found }"
        for k, rule in enumerate(policy.rules):
            value = 1 if rule.mode == "public" else k + 2
            conds = " ".join([fresh, *self._conditions(rule.match), LOOP_OFF])
            rules.append(f"http-request set-var(txn.sy_ad) int({value}) if {conds}")
        default = 1 if policy.default_mode == "public" else len(policy.rules) + 2
        rules.append(f"http-request set-var(txn.sy_ad) int({default}) if {fresh} {LOOP_OFF}")
        rules.append(
            f'http-request set-header {ACCESS_HEADER} "%[var(txn.sy_ad)]" '
            f"if {LOOP_OFF} {{ var(txn.sy_ad) -m int 2: }}"
        )
        # second hop: read the decision back, then check the identity
        rules.append(
            f"http-request return status 403 if {LOOP_ON} !{{ req.hdr({ACCESS_HEADER}) -m found }}"
        )
        rules.append(f"http-request set-var(txn.sy_ad) req.hdr_val({ACCESS_HEADER}) if {LOOP_ON}")
        need = f"{LOOP_ON} {{ var(txn.sy_ad) -m int 2: }}"
        rules.append(
            f"http-request return status 401 if {need} !{{ req.hdr(X-Forwarded-User) -m found }}"
        )
        for k, rule in enumerate(policy.rules):
            if rule.mode == "groups":
                regex = "|".join(sorted(esc.group_regex(g) for g in rule.groups))
                test = f"!{{ req.fhdr(X-Forwarded-Groups) -m reg '(^|,)({regex})(,|$)' }}"
            elif rule.mode == "email_domains":
                regex = "|".join(
                    sorted(esc.hostname(d).replace(".", "[.]") for d in rule.email_domains)
                )
                test = f"!{{ req.hdr(X-Forwarded-Email) -m reg -i '@({regex})$' }}"
            else:
                continue
            here = f"{LOOP_ON} {{ var(txn.sy_ad) -m int {k + 2} }}"
            rules.append(f"http-request return status 403 if {here} {test}")
        rules += self._posture_rules(policy)
        rules += self._step_up_rules(inst)
        ours = policy.identity_headers
        for name, source in (
            (ours.user, "X-Forwarded-User"),
            (ours.email, "X-Forwarded-Email"),
            (ours.groups, "X-Forwarded-Groups"),
        ):
            if name:
                rules.append(
                    f'http-request set-header {esc.header_name(name)} "%[req.fhdr({source})]" '
                    f"if {LOOP_ON}"
                )
        if ours.token:
            rules.append(
                f"http-request set-header {esc.header_name(ours.token)} "
                f"\"%[req.hdr(Authorization),regsub('^Bearer ',)]\" if {LOOP_ON}"
            )
        if ours.token or any(r.posture and r.posture.claims for r in policy.rules):
            rules.append(f"http-request del-header Authorization if {LOOP_ON}")
        rules += self._strip_session_cookies(policy)
        rules += [
            f"http-request del-header {n} if {LOOP_ON}"
            for n in (*PROXY_HEADERS, ACCESS_HEADER, DEVICE_HEADER)
        ]
        return rules

    @staticmethod
    def _strip_session_cookies(policy: dm.AccessPolicySet) -> list[str]:
        """Remove the proxy's session and CSRF cookies from the request the origin gets.

        Covers the cookie itself, its split parts (``name_0``) and the CSRF cookies
        (``name_csrf``, ``name_<hash>_csrf``): every cookie that starts with the cookie name and an
        underscore or equals it. The header is rewritten on the loopback hop only; the proxy needs
        the cookies on the first hop.
        """
        name = esc.cookie_name(policy.oidc.cookie_name)
        pattern = f"(^|; *){name}(_[A-Za-z0-9]+)*=[^;]*"
        return [
            "http-request set-header Cookie "
            f"\"%[req.fhdr(cookie),regsub('{pattern}','',g),regsub('^; *','')]\" "
            f"if {LOOP_ON} {{ req.hdr(cookie) -m found }}",
            f"http-request del-header Cookie if {LOOP_ON} {{ req.fhdr(cookie) -m len 0 }}",
        ]

    def _step_up_rules(self, inst: AuthInstance) -> list[str]:
        """Rules that ask for more factors (``min_factors``) on the second hop.

        A session that lacks ``sy-factors-<n>`` is sent to the login gate's ``elevate`` address
        (the broker's public URL, deployment configuration) with the host and path to come back
        to; a request that cannot be redirected gets 403.
        """
        rules: list[str] = []
        for k, rule in enumerate(inst.policy.rules):
            if not rule.min_factors:
                continue
            n = int(rule.min_factors)
            here = f"{LOOP_ON} {{ var(txn.sy_ad) -m int {k + 2} }}"
            short = f"!{{ req.fhdr(X-Forwarded-Groups) -m reg '(^|,)sy-factors-{n}(,|$)' }}"
            gate = f"{inst.issuer(self.broker_url)}/_gate/elevate"
            target = (
                f'"{esc.arg(gate)}?level={n}&host=%[req.hdr(host),field(1,:),url_enc]'
                '&path=%[pathq,url_enc]"'
            )
            rules += [
                f"http-request redirect location {target} code 303 "
                f"if {here} {short} {{ method GET HEAD }}",
                f"http-request return status 403 if {here} {short}",
            ]
        return rules

    @staticmethod
    def _posture_rules(policy: dm.AccessPolicySet) -> list[str]:
        """Device conditions on the second hop: the client certificate flag and ID-token claims."""
        rules: list[str] = []
        if any(r.posture and r.posture.device_certificate for r in policy.rules):
            rules.append(
                f"http-request set-header {DEVICE_HEADER} 1 if {LOOP_OFF} {{ ssl_c_used }}"
            )
        if any(r.posture and r.posture.claims for r in policy.rules):
            rules.append(f"http-request set-var(txn.sy_idt) http_auth_bearer if {LOOP_ON}")
        for k, rule in enumerate(policy.rules):
            if rule.posture is None:
                continue
            here = f"{LOOP_ON} {{ var(txn.sy_ad) -m int {k + 2} }}"
            if rule.posture.device_certificate:
                test = f"!{{ req.hdr({DEVICE_HEADER}) -m str 1 }}"
                rules.append(f"http-request return status 403 if {here} {test}")
            for claim in rule.posture.claims:
                fetch = f"var(txn.sy_idt),jwt_payload_query('$.{claim.name}')"
                if claim.op == "present":
                    test = f"!{{ {fetch} -m found }}"
                else:
                    # a JSON boolean reaches the comparison as 1 or 0 (seen with a real token)
                    wanted = (
                        ("true",) if claim.op == "true" else claim.values or (claim.value or "",)
                    )
                    options = " ".join(esc.quoted(v) for v in _claim_forms(wanted))
                    test = f"!{{ {fetch} -m str {options} }}"
                rules.append(f"http-request return status 403 if {here} {test}")
        return rules

    @staticmethod
    def _auth_routing(inst: AuthInstance) -> list[str]:
        """Send login endpoints and requests that need a login to the proxy (first hop only)."""
        prefix = esc.arg(inst.policy.auth_path)
        return [
            f"use_backend {inst.backend} if {LOOP_OFF} {{ path_beg {prefix}/ }}",
            f"use_backend {inst.backend} if {LOOP_OFF} {{ var(txn.sy_ad) -m int 2: }}",
        ]

    def _request_rules(
        self,
        svc: dm.VirtualService,
        ports: list[dm.VsPort],
        allow: dm.AllowListPolicySet | None,
        deny: dm.AllowListPolicySet | None = None,
        inst: AuthInstance | None = None,
    ) -> list[str]:
        rules: list[str] = []
        if inst is not None:
            rules += self._loop_entry(inst)
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
        if deny is not None:
            # After the client address is restored (behind a CDN ``src`` is the real client), and
            # before anything else touches the request.
            rules.append(
                f"http-request deny deny_status 403 if {{ src -f {self._list_path(deny)} }}"
            )
        rules += [
            "http-request del-header X-Forwarded-For",
            f"http-request set-header X-Forwarded-Proto https if {{ ssl_fc }}{self._g()}",
            f"http-request set-header X-Forwarded-Proto http if !{{ ssl_fc }}{self._g()}",
            "http-request set-var(txn.host) req.hdr(host),field(1,:),lower",
        ]
        for port in ports:
            if port.https_redirect_port is not None:
                suffix = "" if port.https_redirect_port == 443 else f":{port.https_redirect_port}"
                rules.append(
                    f'http-request redirect prefix "https://%[var(txn.host)]{suffix}" code 301 '
                    f"if {{ dst_port {port.port} }}"
                )
        if any(r.pools for r in svc.routes):
            rules.append("http-request set-var(txn.pick) rand(100)")
        if inst is not None:
            rules += self._auth_hygiene(inst)
        rules += self._limit_rules(svc)
        redirects, responses, rewrites, _ = self._policy_stages(svc)
        rules += redirects
        ordered = sorted(enumerate(svc.routes), key=lambda ir: (ir[1].order, ir[0]))
        rules += [
            self._redirect_line(r.match, r.redirect) for _, r in ordered if r.redirect is not None
        ]
        rules += responses
        rules += self._jwt_rules(svc)
        rules += self._cert_rules(svc)
        rules += rewrites
        if inst is not None:
            rules += self._access_rules(inst)
        return rules

    @staticmethod
    def _conditions(match: dm.RouteMatch) -> list[str]:
        """The ACL conditions of a match, ANDed (an empty match gives none).

        Header and query operands are double-quoted after validation; the wildcard regexes are
        single-quoted so ``$`` is never expanded.
        """
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
        if match.methods:
            conds.append("{ method " + " ".join(sorted(m.value for m in match.methods)) + " }")
        for header in match.headers:
            fetch = f"req.hdr({esc.header_name(header.name)})"
            if header.op is dm.HeaderOp.PRESENT:
                conds.append(f"{{ {fetch} -m found }}")
            elif header.op is dm.HeaderOp.ABSENT:
                conds.append(f"!{{ {fetch} -m found }}")
            else:
                value = esc.quoted(_need(header.value, "header value"))
                conds.append(f"{{ {fetch} -m {_HEADER_FLAGS[header.op]} {value} }}")
        for param in match.query:
            fetch = f"url_param({esc.query_param(param.name)})"
            if param.op is dm.QueryOp.PRESENT:
                conds.append(f"{{ {fetch} -m found }}")
            else:
                conds.append(f"{{ {fetch} -m str {esc.quoted(_need(param.value, 'value'))} }}")
        if match.source_cidrs:
            nets = sorted(match.source_cidrs, key=lambda n: (n.version, int(n.network_address)))
            conds.append("{ src " + " ".join(str(n) for n in nets) + " }")
        if match.sni:
            if _is_wild(match.sni):
                regex = _quote_regex(esc.wildcard_regex(match.sni))
                conds.append(f"{{ ssl_fc_sni -m reg {regex} }}")
            else:
                conds.append(f"{{ ssl_fc_sni -m str {esc.hostname(match.sni)} }}")
        return conds

    def _failover_rules(self, route: dm.VsRoute, cond: str) -> list[str]:
        """Send to the first pool of the chain that still has a server up (ADR 0119)."""
        chain = [_need(route.pool_id, "route pool"), *route.fallback_pool_ids]
        rules: list[str] = []
        for i, pool_id in enumerate(chain):
            backend = self._backend_name(pool_id)
            if i == len(chain) - 1:
                rules.append(f"use_backend {backend} if {cond}")
            else:
                rules.append(f"use_backend {backend} if {cond} {{ nbsrv({backend}) gt 0 }}")
        return rules

    def _weighted_rules(self, route: dm.VsRoute, cond: str) -> list[str]:
        """Split a route over its pools by the request's draw in ``txn.pick`` (ADR 0119)."""
        rules: list[str] = []
        cumulative = 0
        for i, target in enumerate(route.pools):
            backend = self._backend_name(target.pool_id)
            cumulative += target.weight
            if i == len(route.pools) - 1:
                rules.append(f"use_backend {backend} if {cond}")
            else:
                rules.append(
                    f"use_backend {backend} if {cond} {{ var(txn.pick) -m int lt {cumulative} }}"
                )
        return rules

    def _routing_rules(self, svc: dm.VirtualService, name: str) -> list[str]:
        rules: list[str] = []
        pool_routes = [(i, r) for i, r in enumerate(svc.routes) if r.pool_ids]
        # Weighted routes are conditional even on a bare host: the map files hold one pool per key.
        path_routes = [
            (i, r)
            for i, r in pool_routes
            if r.pools or r.fallback_pool_ids or not r.match.is_host_only
        ]
        for _, route in sorted(path_routes, key=lambda ir: (ir[1].order, ir[0])):
            cond = " ".join(self._conditions(route.match))
            if route.pools:
                rules += self._weighted_rules(route, cond)
                continue
            backend = self._backend_name(_need(route.pool_id, "route pool"))
            if route.fallback_pool_ids:
                rules += self._failover_rules(route, cond)
                continue
            rules.append(f"use_backend {backend} if {cond}")
        exact: dict[str, str] = {}
        wild: dict[str, str] = {}
        for _, route in pool_routes:
            host = route.match.host
            if (
                host is None
                or route.pools
                or route.fallback_pool_ids
                or not route.match.is_host_only
            ):
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
            lines += self._persistence_lines(pool.persistence, self.group.peers is not None)
        if pool.tunnel_timeout_s is not None:
            lines.append(f"timeout tunnel {pool.tunnel_timeout_s}s")
        if monitor is not None:
            lines += self._monitor_lines(monitor)
        servers = sorted(self._server(pool, m, monitor, mode) for m in pool.members)
        return {"name": self._backend_name(pool.id), "lines": lines, "servers": servers}

    @staticmethod
    def _persistence_lines(persistence: dm.Persistence, peers: bool) -> list[str]:
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
        if peers:
            table += f" peers {PEERS_SECTION}"
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
