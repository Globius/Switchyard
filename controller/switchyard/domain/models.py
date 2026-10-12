"""Domain models: the desired-state objects a tenant owns.

Every model is immutable (``frozen``), rejects unknown fields and validates its own invariants.
Rules that span objects (references, VIP placement, port collisions, certificate coverage) live in
:mod:`switchyard.domain.snapshot`.

The models mirror the tables in the backend design ("Data model"). Cross-object references are by
id, never by embedding, so a reference can be checked and a change set can touch one object.

Example:
    >>> import uuid
    >>> net = Network(id=uuid.UUID(int=1), tenant_id=uuid.UUID(int=2), name="lb",
    ...               cidr="192.0.2.0/24", gateway="192.0.2.1")
    >>> str(net.cidr)
    '192.0.2.0/24'
"""

import enum
import ipaddress
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from switchyard.domain import validators as v

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

Name = Annotated[str, AfterValidator(v.validate_name)]
Hostname = Annotated[str, AfterValidator(v.validate_hostname)]
HostPattern = Annotated[str, AfterValidator(v.validate_host_pattern)]
Port = Annotated[int, AfterValidator(v.validate_port)]
UrlPath = Annotated[str, AfterValidator(v.validate_path)]
Location = Annotated[str, AfterValidator(v.validate_location)]
HeaderName = Annotated[str, AfterValidator(v.validate_header_name)]
MediaType = Annotated[str, AfterValidator(v.validate_media_type)]
CookieName = Annotated[str, AfterValidator(v.validate_cookie_name)]
MatchText = Annotated[str, AfterValidator(v.validate_match_text)]
SecretRef = Annotated[str, AfterValidator(v.validate_secret_ref)]
HeaderValue = Annotated[str, AfterValidator(v.validate_header_value)]
QueryParam = Annotated[str, AfterValidator(v.validate_query_param)]
Body = Annotated[str, AfterValidator(v.validate_body)]
DiscoveryTag = Annotated[str, AfterValidator(v.validate_discovery_tag)]


class HaMode(enum.StrEnum):
    """How an SE group places its VIPs."""

    BGP = "bgp"
    VRRP = "vrrp"


class NetworkRole(enum.StrEnum):
    """What an SE group uses one of its networks for; drives the VM NICs."""

    VIP = "vip"
    BACKEND = "backend"
    MGMT = "mgmt"


class Protocol(enum.StrEnum):
    """Virtual service protocol. ``udp`` renders to IPVS via Keepalived, not HAProxy (M6)."""

    HTTP = "http"
    TCP = "tcp"
    UDP = "udp"


class Algorithm(enum.StrEnum):
    """Pool load-balancing algorithm."""

    ROUNDROBIN = "roundrobin"
    LEASTCONN = "leastconn"
    SOURCE = "source"
    RANDOM = "random"


class AdminState(enum.StrEnum):
    """Operator-controlled pool member state."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    DRAIN = "drain"


class MonitorType(enum.StrEnum):
    """Health monitor kind."""

    TCP = "tcp"
    HTTP = "http"
    UDP = "udp"


class CertSource(enum.StrEnum):
    """Where a certificate came from."""

    UPLOAD = "upload"
    CLOUDFLARE_ORIGIN = "cloudflare_origin"
    ACME = "acme"
    STEP_CA = "step_ca"


class AcmeChallenge(enum.StrEnum):
    """How an ACME certificate authority checks control of a name."""

    DNS01 = "dns-01"
    HTTP01 = "http-01"
    TLSALPN01 = "tls-alpn-01"


class TlsProfile(enum.StrEnum):
    """Protocol versions and ciphers a TLS port offers (Mozilla's server-side TLS guidelines).

    ``modern``: TLS 1.3 only. ``intermediate``: TLS 1.2 and 1.3 with forward-secret AEAD ciphers.
    ``legacy``: TLS 1.0 to 1.3 for old clients; use it only while such clients exist. A port
    without a profile keeps the engine default: TLS 1.2 or later with HAProxy's own cipher list.
    """

    MODERN = "modern"
    INTERMEDIATE = "intermediate"
    LEGACY = "legacy"


class RenewPolicy(enum.StrEnum):
    """Whether the controller renews the certificate."""

    MANUAL = "manual"
    AUTO = "auto"


RedirectCode = Literal[301, 302, 303, 307, 308]


class Model(BaseModel):
    """Base class: immutable, strict about unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Entity(Model):
    """A persisted object: UUIDv7 ``id`` and an optimistic-locking ``version``."""

    id: UUID
    version: int = Field(default=1, ge=1)


class TenantOwned(Entity):
    """An object owned by one tenant, with a name that is unique per tenant and kind."""

    tenant_id: UUID
    name: Name


class Tenant(Entity):
    """Root of ownership. The ``system`` tenant holds shared objects."""

    name: Name
    quotas: dict[str, int] = Field(default_factory=dict)
    require_approval: bool = False


class Network(TenantOwned):
    """An L2/L3 network VMs attach to.

    Attributes:
        cidr: Network prefix; host bits must be zero.
        gateway: Optional gateway, must lie inside ``cidr``.
        managed: True when the controller created the Proxmox SDN objects, False when attached.
    """

    cidr: IPNetwork
    gateway: IPAddress | None = None
    sdn_zone: Name | None = None
    vnet: Name | None = None
    vlan_or_vni: int | None = Field(default=None, ge=1, le=16_777_215)
    managed: bool = False

    @model_validator(mode="after")
    def _gateway_inside(self) -> Self:
        if self.gateway is not None:
            v.validate_vip_in_network(self.gateway, self.cidr)
        return self


class IpRange(Model):
    """An inclusive address range inside a VIP pool."""

    start: IPAddress
    end: IPAddress

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start.version != self.end.version:
            raise ValueError("range start and end must be the same IP version")
        if int(self.start) > int(self.end):
            raise ValueError("range start must not be after end")
        return self

    def __contains__(self, item: object) -> bool:
        """Return True if ``item`` is an address inside the range."""
        if not isinstance(item, ipaddress.IPv4Address | ipaddress.IPv6Address):
            return False
        return item.version == self.start.version and int(self.start) <= int(item) <= int(self.end)


class VipPool(TenantOwned):
    """Address ranges VIPs are allocated from, on one network."""

    network_id: UUID
    ranges: tuple[IpRange, ...] = Field(min_length=1)


class SeGroupNetwork(Model):
    """A network attached to an SE group, with its role."""

    network_id: UUID
    role: NetworkRole


class PeerMember(Model):
    """One service engine in a peer group: its HAProxy host name and its backend-network address.

    Example:
        >>> PeerMember(name="se-1", address="10.20.0.5").name
        'se-1'
    """

    name: Name
    address: IPAddress


class PeerSync(Model):
    """Share persistence tables between the engines of a group (HAProxy ``peers``; ADR 0118).

    Attributes:
        port: TCP port the engines use to talk to each other, on the backend network.
        members: Every engine of the group, by the name HAProxy finds as its own (the host name)
            and the address it listens on. At least two, at most 16.
    """

    port: Port = 10_000
    members: tuple[PeerMember, ...] = Field(min_length=2, max_length=16)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [m.name for m in self.members]
        addresses = [m.address for m in self.members]
        if len(set(names)) != len(names) or len(set(addresses)) != len(addresses):
            raise ValueError("peer names and addresses must be unique")
        return self


class SeGroup(TenantOwned):
    """A set of HAProxy service engines that serve the same virtual services.

    Attributes:
        ha_mode: How VIPs are placed (BGP or VRRP).
        networks: Attached networks; VIPs must live in a ``vip`` network.
        maxconn: HAProxy global ``maxconn``.
        routing_profile_id: How VIPs are announced: a ``vrrp`` profile for ``ha_mode: vrrp`` and
            a ``bgp`` profile for ``ha_mode: bgp``. Optional until the group is deployed.
        canary_seconds: How long the canary SE is watched after a push before the rest of the
            group is updated (0 skips the wait).
        peers: Persistence table sync between the group's engines; ``None`` keeps each table local.
    """

    ha_mode: HaMode
    networks: tuple[SeGroupNetwork, ...] = Field(min_length=1)
    maxconn: int = Field(default=20_000, ge=1, le=10_000_000)
    canary_seconds: int = Field(default=30, ge=0, le=600)
    routing_profile_id: UUID | None = None
    peers: PeerSync | None = None


InterfaceName = Annotated[str, AfterValidator(v.validate_interface)]


class StaticRoute(Model):
    """A static route installed on every engine of the group."""

    prefix: IPNetwork
    next_hop: IPAddress

    @model_validator(mode="after")
    def _same_family(self) -> Self:
        if self.prefix.version != self.next_hop.version:
            raise ValueError("prefix and next_hop must be the same IP version")
        return self


class BgpPeer(Model):
    """An upstream BGP neighbour (for example a tenant's FortiGate VDOM).

    Attributes:
        peer_ip: Neighbour address.
        remote_asn: Neighbour AS number.
        md5_ref: OpenBao reference of the session password; the password itself is never stored.
        max_prefixes: Prefix limit for this neighbour; overrides the profile's ``max_prefixes``.
    """

    peer_ip: IPAddress
    remote_asn: int = Field(ge=1, le=4_294_967_295)
    md5_ref: SecretRef | None = None
    max_prefixes: int | None = Field(default=None, ge=1, le=10_000_000)


class VrrpProfile(TenantOwned):
    """Keepalived VRRP settings for an SE group.

    Attributes:
        vrid: Virtual router id (1-255); unique per L2 segment.
        interface: Interface that carries VRRP advertisements.
        advert_interval_s: Advertisement interval in seconds.
        base_priority: Priority of the first engine; each next one gets ``priority_step`` less.
        priority_step: Priority decrease per engine.
        static_routes: Routes installed on every engine.
    """

    kind: Literal["vrrp"] = "vrrp"
    vrid: int = Field(ge=1, le=255)
    interface: InterfaceName
    advert_interval_s: int = Field(default=1, ge=1, le=10)
    base_priority: int = Field(default=150, ge=2, le=254)
    priority_step: int = Field(default=10, ge=1, le=50)
    static_routes: tuple[StaticRoute, ...] = ()


class BgpProfile(TenantOwned):
    """FRR BGP settings for an SE group (route health injection, ECMP).

    Attributes:
        asn: The engines' AS number.
        bfd: Run BFD with the peers for sub-second failure detection.
        peers: Upstream neighbours (at least one).
        max_prefixes: Default prefix limit per neighbour (``maximum-prefix``): FRR resets the
            session when a neighbour announces more, and tries again after five minutes.
            ``None`` sets no limit.
        max_paths: ECMP width.
        static_routes: Routes installed on every engine.
    """

    kind: Literal["bgp"] = "bgp"
    asn: int = Field(ge=1, le=4_294_967_295)
    bfd: bool = True
    peers: tuple[BgpPeer, ...] = Field(min_length=1)
    max_prefixes: int | None = Field(default=None, ge=1, le=10_000_000)
    max_paths: int = Field(default=4, ge=1, le=64)
    static_routes: tuple[StaticRoute, ...] = ()


RoutingProfile = Annotated[VrrpProfile | BgpProfile, Field(discriminator="kind")]


class RedirectAction(Model):
    """An HTTP redirect: absolute path or URL, with a status code."""

    location: Location
    code: RedirectCode = 301


class HttpMethod(enum.StrEnum):
    """HTTP request methods a match can name."""

    GET = "GET"
    HEAD = "HEAD"
    POST = "POST"
    PUT = "PUT"
    PATCH = "PATCH"
    DELETE = "DELETE"
    OPTIONS = "OPTIONS"


class HeaderOp(enum.StrEnum):
    """How a header match compares: presence, or a literal comparison of the value."""

    PRESENT = "present"
    ABSENT = "absent"
    EQUALS = "equals"
    PREFIX = "prefix"
    SUFFIX = "suffix"
    CONTAINS = "contains"


class HeaderMatch(Model):
    """Match a request header. ``value`` is required for the comparisons and forbidden otherwise.

    Example:
        >>> HeaderMatch(name="X-Env", op="equals", value="canary").op
        <HeaderOp.EQUALS: 'equals'>
    """

    name: HeaderName
    op: HeaderOp = HeaderOp.EQUALS
    value: HeaderValue | None = None

    @model_validator(mode="after")
    def _value_for_op(self) -> Self:
        has_value = self.op not in (HeaderOp.PRESENT, HeaderOp.ABSENT)
        if has_value and self.value is None:
            raise ValueError(f"op {self.op.value} needs a value")
        if not has_value and self.value is not None:
            raise ValueError(f"op {self.op.value} takes no value")
        return self


class QueryOp(enum.StrEnum):
    """How a query-parameter match compares."""

    PRESENT = "present"
    EQUALS = "equals"


class QueryMatch(Model):
    """Match a query-string parameter (``?name=value``)."""

    name: QueryParam
    op: QueryOp = QueryOp.EQUALS
    value: HeaderValue | None = None

    @model_validator(mode="after")
    def _value_for_op(self) -> Self:
        if self.op is QueryOp.EQUALS and self.value is None:
            raise ValueError("op equals needs a value")
        if self.op is QueryOp.PRESENT and self.value is not None:
            raise ValueError("op present takes no value")
        return self


class RouteMatch(Model):
    """Request match shared by routes and policy rules; every condition given must hold (AND).

    Attributes:
        host: Exact host or single-label ``*.`` wildcard (the ``Host`` header, port removed).
        path_prefix: Path starts with this.
        path_exact: Path equals this. Not together with ``path_prefix``.
        methods: Request method is one of these.
        headers: Request header conditions, all of which must hold.
        query: Query-string parameter conditions, all of which must hold.
        source_cidrs: Client address is inside one of these networks (after client-IP restoration).
        sni: TLS server name the client asked for (exact or ``*.`` wildcard); never true on a
            port without TLS.

    Example:
        >>> RouteMatch(methods=["GET"], path_prefix="/api").is_empty
        False
        >>> RouteMatch().is_empty
        True
    """

    host: HostPattern | None = None
    path_prefix: UrlPath | None = None
    path_exact: UrlPath | None = None
    methods: tuple[HttpMethod, ...] = Field(default=(), max_length=7)
    headers: tuple[HeaderMatch, ...] = Field(default=(), max_length=8)
    query: tuple[QueryMatch, ...] = Field(default=(), max_length=8)
    source_cidrs: tuple[IPNetwork, ...] = Field(default=(), max_length=16)
    sni: HostPattern | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.path_prefix is not None and self.path_exact is not None:
            raise ValueError("use either path_prefix or path_exact, not both")
        if len(set(self.methods)) != len(self.methods):
            raise ValueError("methods must be unique")
        return self

    @property
    def is_empty(self) -> bool:
        """True when the match has no condition (it matches every request)."""
        return not (
            self.host
            or self.path_prefix
            or self.path_exact
            or self.methods
            or self.headers
            or self.query
            or self.source_cidrs
            or self.sni
        )

    @property
    def is_host_only(self) -> bool:
        """True when the host is the only condition (routed through a map file)."""
        return self.host is not None and self.model_copy(update={"host": None}).is_empty


class WeightedPool(Model):
    """One target of a weighted route: a pool and its share of the matching requests.

    Example:
        >>> import uuid
        >>> WeightedPool(pool_id=uuid.UUID(int=1), weight=90).weight
        90
    """

    pool_id: UUID
    weight: int = Field(ge=1, le=99)


class VsRoute(Model):
    """Route a request to a pool, split it over weighted pools, or answer it with a redirect.

    Attributes:
        order: Position among path routes; lower runs first. Host-only routes are looked up in map
            files and are order-independent (exact host wins over wildcard).
        match: What the request must match. Must not be empty.
        pool_id: Target pool, or ``None`` when ``redirect`` or ``pools`` is set.
        redirect: Redirect to answer with, or ``None`` when ``pool_id`` or ``pools`` is set.
        pools: Canary or blue-green split: two to eight pools whose weights add up to 100. Each
            request is sent to one of them at random in that proportion (ADR 0119).

    Example:
        >>> import uuid
        >>> route = VsRoute(
        ...     match=RouteMatch(host="app.example.test"),
        ...     pools=(
        ...         WeightedPool(pool_id=uuid.UUID(int=1), weight=90),
        ...         WeightedPool(pool_id=uuid.UUID(int=2), weight=10),
        ...     ),
        ... )
        >>> len(route.pool_ids)
        2
    """

    order: int = Field(default=0, ge=0)
    match: RouteMatch
    pool_id: UUID | None = None
    redirect: RedirectAction | None = None
    pools: tuple[WeightedPool, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def _one_action(self) -> Self:
        if sum([self.pool_id is not None, self.redirect is not None, bool(self.pools)]) != 1:
            raise ValueError("a route needs exactly one of pool_id, redirect or pools")
        if self.pools:
            if len(self.pools) < 2:
                raise ValueError("a weighted route needs at least two pools")
            if sum(p.weight for p in self.pools) != 100:
                raise ValueError("the weights of a weighted route must add up to 100")
            if len({p.pool_id for p in self.pools}) != len(self.pools):
                raise ValueError("a pool can appear only once in a weighted route")
        if self.match.is_empty:
            raise ValueError("a route must match a host or a path; use default_pool_id as fallback")
        return self

    @property
    def pool_ids(self) -> tuple[UUID, ...]:
        """Every pool the route can send traffic to (none for a redirect)."""
        if self.pools:
            return tuple(p.pool_id for p in self.pools)
        return (self.pool_id,) if self.pool_id is not None else ()


class VsPort(Model):
    """One listening port on a virtual service's VIP.

    Attributes:
        port: Listening port.
        tls: Terminate TLS on this port using the service's certificates.
        http2: Offer HTTP/2 via ALPN (requires ``tls``).
        proxy_protocol: Accept the PROXY protocol header from the client side.
        https_redirect_port: On a plaintext port, redirect every request to ``https`` on this port
            (which must be a TLS port of the same service).
        tls_profile: Protocol versions and ciphers of a TLS port. ``None`` keeps the engine
            default (TLS 1.2 or later, HAProxy's default ciphers).
        client_ca_id: A ``client_ca`` certificate: clients must present a certificate it signed
            (mutual TLS). Needs ``tls``.
        client_verify: With ``client_ca_id``: ``required`` refuses clients without a valid
            certificate; ``optional`` lets them in and the service can look at ``ssl_c_verify``.
        tls_passthrough: ``tcp`` services only: do not terminate TLS; route by the SNI in the
            client hello (ADR 0115). Excludes every TLS option of the port.
        http3: Also listen for HTTP/3 over QUIC on the same number (UDP) and advertise it with
            ``Alt-Svc`` (ADR 0116). Needs ``tls`` and ``http2``.
    """

    port: Port
    tls: bool = False
    http2: bool = False
    proxy_protocol: bool = False
    https_redirect_port: Port | None = None
    tls_profile: TlsProfile | None = None
    client_ca_id: UUID | None = None
    client_verify: Literal["required", "optional"] = "required"
    tls_passthrough: bool = False
    http3: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.tls_passthrough and (
            self.tls
            or self.http2
            or self.http3
            or self.client_ca_id is not None
            or self.tls_profile is not None
            or self.https_redirect_port is not None
        ):
            raise ValueError("tls_passthrough excludes tls, http2, http3 and the other TLS options")
        if self.http3 and not (self.tls and self.http2):
            raise ValueError("http3 requires tls and http2")
        if self.client_ca_id is not None and not self.tls:
            raise ValueError("client_ca_id requires tls")
        if self.http2 and not self.tls:
            raise ValueError("http2 requires tls")
        if self.tls_profile is not None and not self.tls:
            raise ValueError("tls_profile requires tls")
        if self.https_redirect_port is not None and self.tls:
            raise ValueError("https_redirect_port is only valid on a plaintext port")
        return self


class Compression(Model):
    """Compress responses for clients that accept it (HAProxy ``compression``), ``http`` only.

    Attributes:
        algorithms: In order of preference: ``gzip``, ``deflate``.
        content_types: Media types to compress, such as ``text/html``. Never images or archives.

    Example:
        >>> Compression().algorithms
        ('gzip',)
    """

    algorithms: tuple[Literal["gzip", "deflate"], ...] = Field(default=("gzip",), min_length=1)
    content_types: tuple[MediaType, ...] = Field(
        default=(
            "text/html",
            "text/plain",
            "text/css",
            "text/javascript",
            "application/javascript",
            "application/json",
            "application/xml",
            "image/svg+xml",
        ),
        min_length=1,
        max_length=32,
    )

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.algorithms)) != len(self.algorithms):
            raise ValueError("algorithms must be unique")
        return self


class VirtualService(TenantOwned):
    """A VIP, its ports and how traffic reaches pools.

    Attributes:
        se_group_id: The SE group that serves it.
        protocol: ``http`` (L7 with host/path routing), ``tcp`` (L4) or ``udp`` (IPVS).
        enabled: Disabled services are not rendered.
        vip: The bind address. ``vip_pool_id`` records which pool it was allocated from.
        ports: Listening ports (at least one, unique).
        routes: L7 routes (``http`` only).
        default_pool_id: Fallback pool for ``http``; the only pool for ``tcp``/``udp``.
        certificate_ids: Certificates for TLS ports. The first is the default for unknown SNI.
        policy_set_ids: Redirect, rewrite, respond and error-page policy sets, in attachment order.
        allow_list_id: Allow-list policy set (for example the Cloudflare ranges).
        enforce_allow_list: Reject connections from sources outside the allow list.
        restore_client_ip_header: Header (for example ``CF-Connecting-IP``) that carries the real
            client address; honoured only when the connection source is in the allow list.
        compression: Response compression (``http`` only; ADR 0117).
    """

    se_group_id: UUID
    protocol: Protocol
    enabled: bool = True
    vip: IPAddress
    vip_pool_id: UUID | None = None
    ports: tuple[VsPort, ...] = Field(min_length=1)
    routes: tuple[VsRoute, ...] = ()
    default_pool_id: UUID | None = None
    certificate_ids: tuple[UUID, ...] = ()
    policy_set_ids: tuple[UUID, ...] = ()
    allow_list_id: UUID | None = None
    enforce_allow_list: bool = False
    restore_client_ip_header: HeaderName | None = None
    compression: Compression | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        numbers = [p.port for p in self.ports]
        if len(set(numbers)) != len(numbers):
            raise ValueError("ports must be unique")
        if self.protocol is Protocol.UDP:
            # Fields a UDP service cannot use are rejected per field by the snapshot validator
            # (``errors[].path`` addresses each one); only the one it needs is required here.
            if self.default_pool_id is None:
                raise ValueError("a udp service needs default_pool_id")
            return self
        if self.protocol is Protocol.HTTP and any(p.tls_passthrough for p in self.ports):
            raise ValueError("tls_passthrough is only valid for tcp services")
        if any(p.tls for p in self.ports) and not self.certificate_ids:
            raise ValueError("a TLS port needs at least one certificate")
        if self.protocol is Protocol.HTTP:
            if not self.routes and self.default_pool_id is None:
                raise ValueError("an http service needs routes or default_pool_id")
        else:
            passthrough = all(p.tls_passthrough for p in self.ports)
            if self.policy_set_ids or (self.routes and not passthrough):
                raise ValueError(
                    f"a {self.protocol} service cannot have routes or policy sets "
                    "(routes need every port to be tls_passthrough)"
                )
            if self.compression is not None:
                raise ValueError("compression is only valid for http services")
            if passthrough and self.protocol is not Protocol.TCP:
                raise ValueError("tls_passthrough is only valid for tcp services")
            if self.default_pool_id is None:
                raise ValueError(f"a {self.protocol} service needs default_pool_id")
            if any(p.http2 or p.http3 or p.https_redirect_port is not None for p in self.ports):
                raise ValueError(
                    "http2, http3 and https_redirect_port are only valid for http services"
                )
            if self.restore_client_ip_header is not None:
                raise ValueError("restore_client_ip_header is only valid for http services")
        if (self.enforce_allow_list or self.restore_client_ip_header) and not self.allow_list_id:
            raise ValueError("enforce_allow_list and restore_client_ip_header need allow_list_id")
        tls_ports = {p.port for p in self.ports if p.tls}
        for p in self.ports:
            if p.https_redirect_port is not None and p.https_redirect_port not in tls_ports:
                raise ValueError(
                    f"https_redirect_port {p.https_redirect_port} is not a TLS port of this service"
                )
        return self


class PersistenceSourceIp(Model):
    """Pin a client address to one member (HAProxy ``stick on src``)."""

    type: Literal["source_ip"] = "source_ip"
    timeout_s: int = Field(default=1800, ge=1, le=86_400)
    table_size: int = Field(default=100_000, ge=1_000, le=10_000_000)


class PersistenceCookie(Model):
    """Pin a client to one member with an HTTP cookie (HAProxy ``cookie``), ``http`` pools only.

    Attributes:
        name: Cookie name.
        mode: ``insert`` makes HAProxy set the cookie; ``rewrite`` rewrites a cookie the member
            sets; ``prefix`` prefixes the member's own cookie value.
        ttl_s: ``insert`` only: lifetime of the cookie in seconds (``maxlife``); omitted gives a
            session cookie.
        secure: ``insert`` only: add the ``Secure`` attribute.
        http_only: ``insert`` only: add the ``HttpOnly`` attribute.

    Example:
        >>> PersistenceCookie(name="SY_SRV", mode="insert", ttl_s=3600).mode
        'insert'
    """

    type: Literal["http_cookie"] = "http_cookie"
    name: CookieName
    mode: Literal["insert", "rewrite", "prefix"] = "insert"
    ttl_s: int | None = Field(default=None, ge=1, le=31_536_000)
    secure: bool = False
    http_only: bool = True

    @model_validator(mode="after")
    def _insert_only(self) -> Self:
        if self.mode != "insert" and (self.ttl_s is not None or self.secure):
            raise ValueError("ttl_s and secure only apply to mode insert")
        return self


class PersistenceHeader(Model):
    """Pin requests that carry the same value of a header to one member (``stick on hdr``).

    ``http`` pools only. Requests without the header fall back to the load-balancing algorithm.

    Example:
        >>> PersistenceHeader(header="X-Session").timeout_s
        1800
    """

    type: Literal["header"] = "header"
    header: HeaderName
    timeout_s: int = Field(default=1800, ge=1, le=86_400)
    table_size: int = Field(default=100_000, ge=1_000, le=10_000_000)


class PersistenceUri(Model):
    """Pin requests with the same URI path, or the same query parameter, to one member.

    ``http`` pools only.

    Attributes:
        source: ``path`` sticks on the request path; ``url_param`` on the value of query
            parameter ``param``.
        param: The query parameter name, required for ``url_param`` and forbidden for ``path``.

    Example:
        >>> PersistenceUri(source="url_param", param="session").source
        'url_param'
    """

    type: Literal["uri"] = "uri"
    source: Literal["path", "url_param"] = "path"
    param: Name | None = None
    timeout_s: int = Field(default=1800, ge=1, le=86_400)
    table_size: int = Field(default=100_000, ge=1_000, le=10_000_000)

    @model_validator(mode="after")
    def _param(self) -> Self:
        if (self.source == "url_param") != (self.param is not None):
            raise ValueError("param is required for source url_param and forbidden for path")
        return self


Persistence = Annotated[
    PersistenceSourceIp | PersistenceCookie | PersistenceHeader | PersistenceUri,
    Field(discriminator="type"),
]
HTTP_ONLY_PERSISTENCE = (PersistenceCookie, PersistenceHeader, PersistenceUri)


class PassiveCheck(Model):
    """Judge members by their real traffic as well as by the health monitor (HAProxy ``observe``).

    Attributes:
        observe: ``layer4`` watches connection failures; ``layer7`` also watches HTTP responses
            (``http`` pools only).
        error_limit: Consecutive errors that trigger ``on_error``.
        on_error: What to do at the limit: ``mark-down``, ``fastinter`` (check faster),
            ``fail-check`` (count one failed check) or ``sudden-death`` (fail and mark down).

    Example:
        >>> PassiveCheck().on_error
        'mark-down'
    """

    observe: Literal["layer4", "layer7"] = "layer7"
    error_limit: int = Field(default=10, ge=1, le=1000)
    on_error: Literal["mark-down", "fastinter", "fail-check", "sudden-death"] = "mark-down"


class PoolMember(Model):
    """One backend server.

    Attributes:
        id: Stable id, used by the runtime enable/disable/drain endpoint.
        address: Backend IP address.
        port: Backend port, or ``None`` to reuse the port the client connected to.
        weight: 0-256; 0 takes no new connections.
        admin_state: Operator state; ``disabled`` and ``drain`` take no new connections.
        name: HAProxy server name; derived from address and port when omitted.
        backup: A backup member takes traffic only when every non-backup member is down.
        maxconn: Most concurrent connections HAProxy sends to this member; extra ones queue.
        drain_timeout_s: With ``admin_state`` ``drain``: seconds after which the controller moves
            the member to ``disabled`` (a periodic job, ADR 0109). ``None`` drains until an
            operator acts. Never reaches the engine configuration.
    """

    id: UUID
    address: IPAddress
    port: Port | None = None
    weight: int = Field(default=100, ge=0, le=256)
    admin_state: AdminState = AdminState.ENABLED
    name: Name | None = None
    backup: bool = False
    maxconn: int | None = Field(default=None, ge=1, le=1_000_000)
    drain_timeout_s: int | None = Field(default=None, ge=1, le=604_800)

    @property
    def server_name(self) -> str:
        """Stable HAProxy server name.

        Example:
            >>> import uuid
            >>> PoolMember(id=uuid.UUID(int=1), address="10.0.0.5", port=8080).server_name
            '10-0-0-5-8080'
        """
        if self.name is not None:
            return self.name
        base = str(self.address).replace(".", "-").replace(":", "-")
        return f"{base}-{self.port}" if self.port is not None else base


class PoolDiscovery(Model):
    """Fill a pool's members from Proxmox guests that carry a tag (ADR 0145).

    The discovery job lists running guests with ``tag``, takes the first address of each that lies
    in ``cidr`` and keeps ``Pool.members`` in step through a normal change set. Members that were
    discovered keep the weight and admin state an operator gave them.

    Attributes:
        source: Where members come from; only ``proxmox_tag`` exists.
        tag: The Proxmox tag; it must start with ``sy-<tenant>.``.
        cidr: Backend network; a guest's address in it becomes the member address.
        port: Member port.
        weight: Weight of a newly discovered member.

    Example:
        >>> PoolDiscovery(tag="sy-acme.web", cidr="10.20.0.0/24", port=8080).weight
        100
    """

    source: Literal["proxmox_tag"] = "proxmox_tag"
    tag: DiscoveryTag
    cidr: IPNetwork
    port: Port
    weight: int = Field(default=100, ge=0, le=256)


class Pool(TenantOwned):
    """A group of backend servers.

    Attributes:
        algorithm: Load-balancing algorithm.
        persistence: Optional persistence profile.
        health_monitor_id: Optional shared health monitor.
        tls: Connect to members over TLS.
        tls_verify: Verify the member certificate against the system CA store. Off by default to
            match Avi behaviour for internal backends with private certificates.
        passive_check: Also judge members by their live traffic. Needs a health monitor.
        slow_start_s: Seconds over which a member that comes back up ramps from zero to its full
            weight (HAProxy ``slowstart``). Needs a health monitor.
        backend_protocol: How HAProxy speaks to the members (``http`` pools only): ``http1``,
            ``h2`` (HTTP/2 negotiated over TLS, needs ``tls``) or ``h2c`` (HTTP/2 without TLS,
            needs no ``tls``). gRPC backends need one of the HTTP/2 kinds (ADR 0116).
        tunnel_timeout_s: Idle time of an upgraded connection (WebSocket) or a long HTTP/2
            stream (gRPC) before HAProxy closes it (``timeout tunnel``; ADR 0116). ``None``
            keeps the default of one hour.
        send_proxy: Send the PROXY protocol header (``v1`` or ``v2``) to every member.
        client_certificate_id: Server certificate HAProxy presents to members (backend
            mutual TLS). Needs ``tls``.
        discovery: Fill ``members`` from tagged Proxmox guests instead of by hand.
        members: Backend servers, unique by name. With ``discovery`` the job owns this list.
    """

    algorithm: Algorithm = Algorithm.ROUNDROBIN
    persistence: Persistence | None = None
    health_monitor_id: UUID | None = None
    tls: bool = False
    tls_verify: bool = False
    passive_check: PassiveCheck | None = None
    slow_start_s: int | None = Field(default=None, ge=1, le=3600)
    backend_protocol: Literal["http1", "h2", "h2c"] = "http1"
    tunnel_timeout_s: int | None = Field(default=None, ge=1, le=86_400)
    send_proxy: Literal["v1", "v2"] | None = None
    client_certificate_id: UUID | None = None
    discovery: PoolDiscovery | None = None
    members: tuple[PoolMember, ...] = ()

    @model_validator(mode="after")
    def _members(self) -> Self:
        names = [m.server_name for m in self.members]
        if len(set(names)) != len(names):
            raise ValueError("pool member names must be unique")
        if self.tls_verify and not self.tls:
            raise ValueError("tls_verify requires tls")
        if self.backend_protocol == "h2" and not self.tls:
            raise ValueError("backend_protocol h2 requires tls; use h2c without tls")
        if self.backend_protocol == "h2c" and self.tls:
            raise ValueError("backend_protocol h2c is for pools without tls; use h2")
        if self.client_certificate_id is not None and not self.tls:
            raise ValueError("client_certificate_id requires tls")
        return self


class HeaderExpect(Model):
    """A response header the health check must see, optionally with an exact value.

    Example:
        >>> HeaderExpect(name="X-Health", value="ok").value
        'ok'
    """

    name: HeaderName
    value: MatchText | None = None


class HttpCheck(Model):
    """HTTP health check request and expectation."""

    method: Literal["GET", "HEAD", "OPTIONS"] = "GET"
    path: UrlPath = "/"
    host: Hostname | None = None
    expect_status: tuple[int, ...] = ()
    expect_body: MatchText | None = None
    expect_header: HeaderExpect | None = None

    @model_validator(mode="after")
    def _codes(self) -> Self:
        if any(not 100 <= c <= 599 for c in self.expect_status):
            raise ValueError("expect_status codes must be between 100 and 599")
        chosen = [bool(self.expect_status), self.expect_body is not None]
        chosen.append(self.expect_header is not None)
        if sum(chosen) > 1:
            raise ValueError("use only one of expect_status, expect_body and expect_header")
        return self


class UdpCheck(Model):
    """UDP health probe (Keepalived ``UDP_CHECK``), sent to the member's check port.

    A UDP check always marks the server down on an ICMP port-unreachable. Without ``payload_hex``
    it sends an empty datagram; with ``require_reply`` the server must also answer.

    Attributes:
        payload_hex: Datagram to send, as lowercase hex (at most 256 bytes).
        require_reply: Mark the server down unless it answers the probe.

    Example:
        >>> UdpCheck(payload_hex="00ff", require_reply=True).require_reply
        True
    """

    payload_hex: str | None = Field(default=None, pattern=r"^([0-9a-f]{2}){1,256}$")
    require_reply: bool = False


TcpPreset = Literal["redis", "smtp", "ldap", "mysql"]


class HealthMonitor(TenantOwned):
    """Active health check, shareable across pools.

    Attributes:
        type: ``tcp`` connect check, ``http`` request check or ``udp`` probe (UDP services only).
        port: Port to check. Required when pool members have no port of their own; otherwise
            members are checked on their service port.
        interval_s: Seconds between checks.
        timeout_s: Seconds before a check fails.
        rise: Consecutive successes to mark a server up.
        fall: Consecutive failures to mark a server down.
        http: Request details; required for ``http`` and forbidden for ``tcp`` and ``udp``.
        udp: Optional probe details; only valid for ``udp``.
        preset: Protocol-aware check for a ``tcp`` monitor: ``redis``, ``smtp``, ``ldap`` or
            ``mysql``. Without one a ``tcp`` monitor only connects.
    """

    type: MonitorType
    port: Port | None = None
    interval_s: int = Field(default=5, ge=1, le=300)
    timeout_s: int = Field(default=3, ge=1, le=300)
    rise: int = Field(default=2, ge=1, le=100)
    fall: int = Field(default=3, ge=1, le=100)
    http: HttpCheck | None = None
    udp: UdpCheck | None = None
    preset: TcpPreset | None = None

    @model_validator(mode="after")
    def _http(self) -> Self:
        if self.preset is not None and self.type is not MonitorType.TCP:
            raise ValueError("preset is only valid on a tcp monitor")
        if self.type is MonitorType.HTTP and self.http is None:
            raise ValueError("an http monitor needs an http block")
        if self.type is not MonitorType.HTTP and self.http is not None:
            raise ValueError(f"a {self.type} monitor cannot have an http block")
        if self.type is not MonitorType.UDP and self.udp is not None:
            raise ValueError(f"a {self.type} monitor cannot have a udp block")
        return self


class Certificate(TenantOwned):
    """Certificate metadata. Key material lives in OpenBao; only ``secret_ref`` is stored.

    Attributes:
        source: Where it came from.
        sans: Subject alternative names; wildcards cover one label.
        not_after: Expiry (timezone-aware).
        secret_ref: OpenBao path of the PEM bundle.
        renew_policy: Whether the controller renews it.
        renew_before_days: How many days before ``not_after`` an ``auto`` certificate is
            renewed (the artboard's "Auto, 30 days before").
        ocsp_stapling: Let the engine fetch and staple OCSP responses for this certificate
            (HAProxy ``ocsp-update``). Needs a certificate with an OCSP responder, so not
            ``cloudflare_origin``.
        purpose: ``server`` (a certificate with a key that a port presents) or ``client_ca``
            (trusted CA certificates, no key, that a port uses to check client certificates).
        crl_secret_ref: OpenBao path of a PEM certificate revocation list (``client_ca`` only).
        acme_provider: Name of the ACME provider that issues and renews it (``acme`` only; the
            default provider when empty).
        challenge: ``dns-01`` (the default) or ``http-01`` (``acme`` only; no wildcard names).
        dns_provider: Name of the DNS credential that publishes the DNS-01 records (``dns-01``
            only; chosen by zone when empty).
    """

    source: CertSource
    sans: tuple[HostPattern, ...] = ()
    not_after: datetime
    secret_ref: SecretRef
    renew_policy: RenewPolicy = RenewPolicy.MANUAL
    renew_before_days: int = Field(default=30, ge=1, le=365)
    ocsp_stapling: bool = False
    purpose: Literal["server", "client_ca"] = "server"
    crl_secret_ref: SecretRef | None = None
    acme_provider: Name | None = None
    challenge: AcmeChallenge | None = None
    dns_provider: Name | None = None

    @model_validator(mode="after")
    def _checked(self) -> Self:
        if (self.acme_provider or self.challenge or self.dns_provider) and (
            self.source is not CertSource.ACME
        ):
            raise ValueError("acme_provider, challenge and dns_provider only apply to source acme")
        if self.dns_provider and self.challenge in {AcmeChallenge.HTTP01, AcmeChallenge.TLSALPN01}:
            raise ValueError("dns_provider is only valid with the dns-01 challenge")
        if self.challenge in {AcmeChallenge.HTTP01, AcmeChallenge.TLSALPN01} and any(
            s.startswith("*.") for s in self.sans
        ):
            raise ValueError(f"{self.challenge.value} cannot validate wildcard names; use dns-01")
        if self.purpose == "server" and not self.sans:
            raise ValueError("a server certificate needs at least one name")
        if self.purpose == "client_ca" and (
            self.sans or self.ocsp_stapling or self.renew_policy is not RenewPolicy.MANUAL
        ):
            raise ValueError("a client_ca has no names, no OCSP stapling and no renewal")
        if self.crl_secret_ref is not None and self.purpose != "client_ca":
            raise ValueError("crl_secret_ref is only valid on a client_ca")
        if self.ocsp_stapling and self.source is CertSource.CLOUDFLARE_ORIGIN:
            raise ValueError("Cloudflare Origin CA certificates have no OCSP responder")
        if self.not_after.tzinfo is None:
            raise ValueError("not_after must be timezone-aware")
        if len(set(self.sans)) != len(self.sans):
            raise ValueError("sans must be unique")
        return self


class RedirectRule(Model):
    """Redirect requests that match ``match`` (at least a host or a path)."""

    match: RouteMatch
    redirect: RedirectAction

    @model_validator(mode="after")
    def _non_empty(self) -> Self:
        if self.match.is_empty:
            raise ValueError("a redirect rule must match a host or a path")
        return self


class RedirectPolicySet(TenantOwned):
    """Ordered HTTP redirect rules applied before routing (first match wins)."""

    kind: Literal["redirect"] = "redirect"
    rules: tuple[RedirectRule, ...] = Field(min_length=1)


class AllowListPolicySet(TenantOwned):
    """Source networks, for example the published Cloudflare ranges.

    Attributes:
        cidrs: Networks in the list.
        managed_by: ``cloudflare`` when the daily ``sync_cloudflare_ips`` job maintains the list.
    """

    kind: Literal["allow_list"] = "allow_list"
    cidrs: tuple[IPNetwork, ...] = Field(min_length=1)
    managed_by: Literal["cloudflare"] | None = None


class HeaderVariable(enum.StrEnum):
    """Per-request values a header action can carry, from a fixed list (no expressions)."""

    CLIENT_IP = "client_ip"
    CLIENT_PORT = "client_port"
    HOST = "host"
    VIP = "vip"
    VIP_PORT = "vip_port"
    METHOD = "method"
    TLS_VERSION = "tls_version"
    TLS_CIPHER = "tls_cipher"
    TLS_SNI = "tls_sni"
    REQUEST_ID = "request_id"


class _HeaderAction(Model):
    """Common part of the header actions: a name and exactly one of ``value`` and ``variable``."""

    name: HeaderName
    value: HeaderValue | None = None
    variable: HeaderVariable | None = None

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if (self.value is None) == (self.variable is None):
            raise ValueError("a header action needs exactly one of value or variable")
        return self


class SetHeader(_HeaderAction):
    """Set a header, replacing any existing values."""

    type: Literal["set_header"] = "set_header"


class AddHeader(_HeaderAction):
    """Add a header, keeping any existing values."""

    type: Literal["add_header"] = "add_header"


class RemoveHeader(Model):
    """Remove every value of a header."""

    type: Literal["remove_header"] = "remove_header"
    name: HeaderName


class ReplacePrefix(Model):
    """Replace a path prefix, on path-segment boundaries (request phase only).

    ``/api`` to ``/v2`` turns ``/api`` into ``/v2`` and ``/api/x`` into ``/v2/x``; ``/apix`` is
    left alone. A ``to_prefix`` of ``/`` strips the prefix (``/api/x`` becomes ``/x``, ``/api``
    becomes ``/``).
    """

    type: Literal["replace_prefix"] = "replace_prefix"
    from_prefix: UrlPath
    to_prefix: UrlPath

    @model_validator(mode="after")
    def _no_trailing_slash(self) -> Self:
        if self.from_prefix == "/" or self.from_prefix.endswith("/"):
            raise ValueError("from_prefix must name a directory without a trailing slash")
        if len(self.to_prefix) > 1 and self.to_prefix.endswith("/"):
            raise ValueError("to_prefix must not end with a slash (use / to strip the prefix)")
        return self


RewriteAction = Annotated[
    SetHeader | AddHeader | RemoveHeader | ReplacePrefix, Field(discriminator="type")
]


class RewriteRule(Model):
    """Change a request (before routing) or a response (before it leaves) when ``match`` holds.

    Attributes:
        phase: ``request`` or ``response``. Response rules match on the request that was made.
        match: Conditions; an empty match applies to every request.
        action: What to change.
    """

    phase: Literal["request", "response"] = "request"
    match: RouteMatch = RouteMatch()
    action: RewriteAction

    @model_validator(mode="after")
    def _allowed(self) -> Self:
        action = self.action
        if isinstance(action, ReplacePrefix):
            if self.phase != "request":
                raise ValueError("replace_prefix is only valid in the request phase")
            return self
        name = action.name.lower()
        if name in v.FORBIDDEN_HEADERS:
            raise ValueError(f"header {action.name} cannot be changed by a policy")
        if self.phase == "request":
            if name in v.MANAGED_REQUEST_HEADERS:
                raise ValueError(f"request header {action.name} is managed by Switchyard")
            if name == "host" and not isinstance(action, SetHeader):
                raise ValueError("the Host header can only be set")
        return self


class RewritePolicySet(TenantOwned):
    """Ordered header and URL rewrite rules (ADR 0102).

    Request rules run after redirects and fixed responses and before routing; response rules run
    on the way out. Rules apply in order within a set and sets in attachment order.
    """

    kind: Literal["rewrite"] = "rewrite"
    rules: tuple[RewriteRule, ...] = Field(min_length=1, max_length=64)


ResponseStatus = Literal[200, 202, 204, 400, 401, 403, 404, 405, 410, 429, 451, 500, 502, 503, 504]
ErrorStatus = Literal[400, 403, 408, 500, 502, 503, 504]
ContentType = Literal["text/plain", "text/html", "application/json"]


class ResponseRule(Model):
    """Answer requests that match ``match`` with a fixed response, without asking a pool.

    Attributes:
        match: Conditions; an empty match answers every request (a maintenance page).
        status: Status code.
        content_type: Media type of ``body``; needed with a body.
        body: Response text; omitted for an empty response.
        retry_after_s: Seconds for a ``Retry-After`` header (only with 429 or 503).
    """

    match: RouteMatch = RouteMatch()
    status: ResponseStatus
    content_type: ContentType | None = None
    body: Body | None = Field(default=None, min_length=1)
    retry_after_s: int | None = Field(default=None, ge=1, le=86_400)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.body is None) != (self.content_type is None):
            raise ValueError("body and content_type go together")
        if self.body is not None and self.status == 204:
            raise ValueError("a 204 response has no body")
        if self.retry_after_s is not None and self.status not in (429, 503):
            raise ValueError("retry_after_s is only valid with status 429 or 503")
        return self


class RespondPolicySet(TenantOwned):
    """Fixed responses sent instead of routing (ADR 0103); the first match wins."""

    kind: Literal["respond"] = "respond"
    rules: tuple[ResponseRule, ...] = Field(min_length=1, max_length=64)


class ErrorPage(Model):
    """The body HAProxy sends when it generates the error ``status`` itself."""

    status: ErrorStatus
    content_type: ContentType = "text/html"
    body: Body = Field(min_length=1)


class ErrorPagesPolicySet(TenantOwned):
    """Custom pages for the errors HAProxy generates (no server available, timeouts, ...)."""

    kind: Literal["error_pages"] = "error_pages"
    pages: tuple[ErrorPage, ...] = Field(min_length=1, max_length=7)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        statuses = [p.status for p in self.pages]
        if len(set(statuses)) != len(statuses):
            raise ValueError("one page per status")
        return self


class RateLimitRule(Model):
    """Limit how often one client (or one key) may use the service; excess requests get ``action``.

    Exactly one of ``max_requests`` (per ``period_s``) and ``max_concurrent`` (requests in
    flight) is set. Each rule keeps its own counter table of ``table_size`` entries.

    Attributes:
        match: Requests this rule counts and limits; empty counts every request. Use a path in
            the match for a per-path limit (a login page, an API prefix).
        key: What the counters are kept per: ``client_ip`` or the value of ``header_name``
            (an API key header; requests without the header are not counted).
        max_requests: Requests allowed per ``period_s`` before the action applies.
        period_s: Length of the window for ``max_requests``.
        max_concurrent: Requests allowed at the same time.
        action: ``deny`` answers ``deny_status``; ``tarpit`` holds the request ``tarpit_s``
            seconds, then answers 429; ``silent_drop`` closes the connection without an answer.
        table_size: Number of keys the table holds; the oldest are evicted when it is full.
    """

    match: RouteMatch = RouteMatch()
    key: Literal["client_ip", "header"] = "client_ip"
    header_name: HeaderName | None = None
    max_requests: int | None = Field(default=None, ge=1, le=1_000_000)
    period_s: Literal[1, 10, 60, 300, 3600] = 10
    max_concurrent: int | None = Field(default=None, ge=1, le=100_000)
    action: Literal["deny", "tarpit", "silent_drop"] = "deny"
    deny_status: Literal[403, 429, 503] = 429
    tarpit_s: int = Field(default=5, ge=1, le=30)
    table_size: int = Field(default=10_000, ge=100, le=1_000_000)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.max_requests is None) == (self.max_concurrent is None):
            raise ValueError("set exactly one of max_requests and max_concurrent")
        if (self.key == "header") != (self.header_name is not None):
            raise ValueError("header_name goes with key header, and only with it")
        return self


class RateLimitPolicySet(TenantOwned):
    """Rate and concurrency limits per client or key (ADR 0110).

    A service may carry at most 8 rate-limit rules across its sets.
    """

    kind: Literal["rate_limit"] = "rate_limit"
    rules: tuple[RateLimitRule, ...] = Field(min_length=1, max_length=8)


PolicySet = Annotated[
    RedirectPolicySet
    | AllowListPolicySet
    | RewritePolicySet
    | RespondPolicySet
    | ErrorPagesPolicySet
    | RateLimitPolicySet,
    Field(discriminator="kind"),
]
#: Policy-set classes a virtual service may list in ``policy_set_ids`` (not the allow list).
ATTACHABLE_POLICY_KINDS = (
    RedirectPolicySet,
    RewritePolicySet,
    RespondPolicySet,
    ErrorPagesPolicySet,
    RateLimitPolicySet,
)
