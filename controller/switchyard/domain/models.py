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
import re
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
PersistenceCookieName = Annotated[str, AfterValidator(v.validate_persistence_cookie_name)]
CookieName = Annotated[str, AfterValidator(v.validate_cookie_name)]
MatchText = Annotated[str, AfterValidator(v.validate_match_text)]
SecretRef = Annotated[str, AfterValidator(v.validate_secret_ref)]
HeaderValue = Annotated[str, AfterValidator(v.validate_header_value)]
QueryParam = Annotated[str, AfterValidator(v.validate_query_param)]
Body = Annotated[str, AfterValidator(v.validate_body)]
Issuer = Annotated[str, AfterValidator(v.validate_issuer)]
JwksUrl = Annotated[str, AfterValidator(v.validate_jwks_url)]
ClaimName = Annotated[str, AfterValidator(v.validate_claim_name)]
ClientId = Annotated[str, AfterValidator(v.validate_client_id)]
GroupName = Annotated[str, AfterValidator(v.validate_group_name)]
PublicKeyPem = Annotated[str, AfterValidator(v.validate_public_key_pem)]
CertName = Annotated[str, AfterValidator(v.validate_cert_name)]
Dn = Annotated[str, AfterValidator(v.validate_dn)]
LdapFilter = Annotated[str, AfterValidator(v.validate_ldap_filter)]
LdapAttr = Annotated[str, AfterValidator(v.validate_ldap_attr)]
EmailAddress = Annotated[str, AfterValidator(v.validate_email_address)]
CertPem = Annotated[str, AfterValidator(v.validate_cert_pem)]
CountryCode = Annotated[str, AfterValidator(v.validate_country_code)]
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
        fallback_pool_ids: Priority groups: pools tried in order when every server of the pool
            before them is down (``pool_id`` only; up to three; ADR 0119).
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
    fallback_pool_ids: tuple[UUID, ...] = Field(default=(), max_length=3)

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
        if self.fallback_pool_ids:
            if self.pool_id is None:
                raise ValueError("fallback_pool_ids needs pool_id")
            chain = (self.pool_id, *self.fallback_pool_ids)
            if len(set(chain)) != len(chain):
                raise ValueError("a pool can appear only once in a priority chain")
        if self.match.is_empty:
            raise ValueError("a route must match a host or a path; use default_pool_id as fallback")
        return self

    @property
    def pool_ids(self) -> tuple[UUID, ...]:
        """Every pool the route can send traffic to (none for a redirect)."""
        if self.pools:
            return tuple(p.pool_id for p in self.pools)
        return (self.pool_id, *self.fallback_pool_ids) if self.pool_id is not None else ()


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


#: Most cache memory one service may ask for, and the total for one SE group (ADR 0117).
CACHE_MAX_MB_PER_SERVICE = 256
CACHE_MAX_MB_PER_GROUP = 1024


class CacheSettings(Model):
    """HAProxy small-object cache for the responses of an ``http`` service (ADR 0117).

    Only responses the origin marks cacheable are stored, for ``GET`` and ``HEAD`` requests without
    ``Authorization``; ``Vary`` is honoured. Rate limits and deny rules run before the cache.

    Attributes:
        size_mb: Memory of the cache in megabytes (1 to 256).
        max_object_kb: Largest response body kept, in kilobytes (1 to 8192).
        max_age_s: Longest time an entry is served without asking the origin (1 to 86400).

    Example:
        >>> CacheSettings().size_mb
        16
    """

    size_mb: int = Field(default=16, ge=1, le=CACHE_MAX_MB_PER_SERVICE)
    max_object_kb: int = Field(default=1024, ge=1, le=8192)
    max_age_s: int = Field(default=60, ge=1, le=86_400)


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
        deny_list_id: Allow-list policy set whose networks are refused (for example a country or
            reputation list, ADR 0147). Applies to the client address after restoration.
        restore_client_ip_header: Header (for example ``CF-Connecting-IP``) that carries the real
            client address; honoured only when the connection source is in the allow list.
        compression: Response compression (``http`` only; ADR 0117).
        cache: Response cache (``http`` only; ADR 0117).
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
    deny_list_id: UUID | None = None
    restore_client_ip_header: HeaderName | None = None
    compression: Compression | None = None
    cache: CacheSettings | None = None

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
            if self.compression is not None or self.cache is not None:
                raise ValueError("compression and cache are only valid for http services")
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
    name: PersistenceCookieName
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
    """Source networks: the published Cloudflare ranges, a country list or a reputation feed.

    The same object serves as an allow list (``allow_list_id``) and as a deny list
    (``deny_list_id``) of a virtual service.

    Attributes:
        cidrs: Networks in the list. A list managed by ``geo`` or ``reputation`` may be empty
            until its first sync; the sync never writes an empty list.
        managed_by: ``cloudflare`` when the daily ``sync_cloudflare_ips`` job maintains the list;
            ``geo`` or ``reputation`` when ``sync_ip_lists`` does (ADR 0147).
        countries: ISO 3166 alpha-2 codes whose networks a ``geo`` list holds.
        feed: Name of the operator-configured feed a ``reputation`` list mirrors.

    Example:
        >>> AllowListPolicySet(
        ...     id=UUID(int=1), tenant_id=UUID(int=2), name="no-xx",
        ...     managed_by="geo", countries=("XX",),
        ... ).cidrs
        ()
    """

    kind: Literal["allow_list"] = "allow_list"
    cidrs: tuple[IPNetwork, ...] = ()
    managed_by: Literal["cloudflare", "geo", "reputation"] | None = None
    countries: tuple[CountryCode, ...] = Field(default=(), max_length=250)
    feed: Name | None = None

    @model_validator(mode="after")
    def _managed(self) -> Self:
        geo, reputation = self.managed_by == "geo", self.managed_by == "reputation"
        if not (geo or reputation) and not self.cidrs:
            raise ValueError("cidrs needs at least one network")
        if geo != bool(self.countries):
            raise ValueError("countries is required for, and only valid with, managed_by geo")
        if reputation != (self.feed is not None):
            raise ValueError("feed is required for, and only valid with, managed_by reputation")
        if len(set(self.countries)) != len(self.countries):
            raise ValueError("countries must be unique")
        return self


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


#: OWASP Core Rule Set versions the SE image ships; a policy pins one of them (ADR 0141).
CRS_VERSIONS = ("4.8.0",)


#: Value types an API schema can require of a query or path parameter (ADR 0197).
ApiParamKind = Literal["string", "integer", "number", "boolean", "uuid", "enum"]
_PATH_TEMPLATE = r"^(/([A-Za-z0-9._~-]{1,64}|\{[A-Za-z_][A-Za-z0-9_]{0,31}\})){1,16}$"
API_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


class ApiParam(Model):
    """A parameter of an API operation: a query parameter, or a ``{name}`` of the path (ADR 0197).

    Attributes:
        name: Parameter name.
        kind: ``string`` (up to ``max_length`` characters), ``integer``, ``number``, ``boolean``,
            ``uuid`` or ``enum`` (one of ``values``).
        required: Query parameters only: the request must carry it (path parameters always do).
        values: The allowed values of an ``enum``.
        max_length: Longest accepted ``string`` value.
    """

    name: QueryParam
    kind: ApiParamKind = "string"
    required: bool = False
    values: tuple[Annotated[str, Field(pattern=r"^[A-Za-z0-9._~-]{1,64}$")], ...] = Field(
        default=(), max_length=32
    )
    max_length: int = Field(default=256, ge=1, le=2048)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.kind == "enum") != bool(self.values):
            raise ValueError("values go with kind enum, and only with it")
        if len(set(self.values)) != len(self.values):
            raise ValueError("enum values must be unique")
        return self


class ApiOperation(Model):
    """One request the API accepts: a method and a path template (ADR 0197).

    Attributes:
        method: HTTP method.
        path: Path template such as ``/pets/{id}``; each ``{name}`` is a path parameter.
        path_params: Types of the ``{name}`` parts (default: any non-empty segment without ``/``).
        query: The query parameters the operation accepts; any other one is a violation unless the
            schema allows unknown ones.
        content_types: Media types a request body may have (empty: no check).
        max_body_bytes: Largest ``Content-Length``; a chunked body is refused when this is set
            (empty: the schema default).
    """

    method: Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    path: Annotated[str, Field(pattern=_PATH_TEMPLATE)]
    path_params: tuple[ApiParam, ...] = Field(default=(), max_length=16)
    query: tuple[ApiParam, ...] = Field(default=(), max_length=32)
    content_types: tuple[MediaType, ...] = Field(default=(), max_length=8)
    max_body_bytes: int | None = Field(default=None, ge=0, le=1_073_741_824)

    @property
    def placeholders(self) -> tuple[str, ...]:
        """Names of the ``{name}`` parts of the path, in order."""
        return tuple(re.findall(r"\{([A-Za-z0-9_]+)\}", self.path))

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        names = self.placeholders
        if len(set(names)) != len(names):
            raise ValueError("a path parameter name appears twice in the path")
        declared = [p.name for p in self.path_params]
        if len(set(declared)) != len(declared) or not set(declared) <= set(names):
            raise ValueError("path_params must be unique and name parameters that the path has")
        if any(p.required for p in self.path_params):
            raise ValueError("path parameters are always required; do not set required")
        queries = [q.name for q in self.query]
        if len(set(queries)) != len(queries):
            raise ValueError("query parameter names must be unique")
        if len(set(self.content_types)) != len(self.content_types):
            raise ValueError("content_types must be unique")
        return self


class ApiSchema(Model):
    """The requests an API accepts, as a positive security model for the WAF (ADR 0197).

    A request is checked in HAProxy before the WAF sees it: its method and path must match an
    operation, its query string may contain only that operation's parameters with values of the
    right type, its body type and size must fit. Everything else is a violation.

    Attributes:
        mode: ``detect`` records the violation in the access log and lets the request through;
            ``block`` refuses it (404 unknown path, 405 wrong method, 400 bad parameters, 411 or
            413 body size, 415 body type).
        operations: The accepted operations (at most 500).
        base_path: Prefix of every operation (for example ``/v1``); requests outside it are
            unknown operations.
        allow_unknown_query: Accept query parameters the operation does not list.
        max_body_bytes: Default largest body for operations that set none.
    """

    mode: Literal["detect", "block"] = "detect"
    operations: tuple[ApiOperation, ...] = Field(min_length=1, max_length=500)
    base_path: Annotated[str, Field(pattern=r"^(/[A-Za-z0-9._~-]{1,64}){0,4}$")] = ""
    allow_unknown_query: bool = False
    max_body_bytes: int | None = Field(default=None, ge=0, le=1_073_741_824)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        seen = set()
        for op in self.operations:
            # {a} and {b} in the same position are the same template
            shape = (op.method, re.sub(r"\{[A-Za-z0-9_]+\}", "{}", op.path))
            if shape in seen:
                raise ValueError(f"operation {op.method} {op.path} is listed twice")
            seen.add(shape)
        return self


class WafPolicySet(TenantOwned):
    """Web application firewall for a service: Coraza with the OWASP Core Rule Set (ADR 0140).

    New policies start in ``detect`` mode: requests are inspected and logged, nothing is blocked,
    so a tenant can read the logs before turning blocking on. A service can carry one WAF policy.

    Attributes:
        mode: ``detect`` logs only; ``block`` refuses requests above the anomaly threshold.
        crs_version: CRS release to use; must be one the SE image ships.
        paranoia_level: CRS paranoia level 1 (fewest rules) to 4.
        inbound_threshold: Anomaly score at which a request counts as an attack.
        outbound_threshold: Anomaly score at which a response counts as a leak.
        disabled_rule_ids: CRS rules to switch off (for false positives).
        excluded_paths: Paths that skip inspection (``path_beg`` match), such as upload endpoints.
        inspect_body: Send the request body (up to the engine buffer size) to the WAF too.
        fail_mode: What to do when the WAF does not answer in ``timeout_ms``: ``open`` lets the
            request through, ``closed`` refuses it with 503 (ADR 0142).
        timeout_ms: How long HAProxy waits for the WAF's verdict.
        api: An API schema (ADR 0197): requests that are not among its operations are violations.

    Example:
        >>> WafPolicySet.model_validate(
        ...     {"id": "00000000-0000-0000-0000-000000000001",
        ...      "tenant_id": "00000000-0000-0000-0000-000000000002", "name": "w"}
        ... ).mode
        'detect'
    """

    kind: Literal["waf"] = "waf"
    mode: Literal["detect", "block"] = "detect"
    crs_version: Literal["4.8.0"] = "4.8.0"
    paranoia_level: int = Field(default=1, ge=1, le=4)
    inbound_threshold: int = Field(default=5, ge=1, le=100)
    outbound_threshold: int = Field(default=4, ge=1, le=100)
    disabled_rule_ids: tuple[int, ...] = Field(default=(), max_length=64)
    excluded_paths: tuple[UrlPath, ...] = Field(default=(), max_length=16)
    inspect_body: bool = True
    fail_mode: Literal["open", "closed"] = "open"
    timeout_ms: int = Field(default=100, ge=10, le=2000)
    api: ApiSchema | None = None

    @model_validator(mode="after")
    def _rules(self) -> Self:
        if any(not 1 <= rule_id <= 999_999 for rule_id in self.disabled_rule_ids):
            raise ValueError("rule ids are numbers from 1 to 999999")
        if len(set(self.disabled_rule_ids)) != len(self.disabled_rule_ids):
            raise ValueError("disabled_rule_ids must be unique")
        if len(set(self.excluded_paths)) != len(self.excluded_paths):
            raise ValueError("excluded_paths must be unique")
        return self


class OidcSettings(Model):
    """OpenID Connect login against a tenant's identity provider (ADR 0130).

    Attributes:
        issuer: The provider's issuer URL (``https``); discovery is read from it. Or:
        directory_id: a ``directory`` policy set; the broker is the issuer (ADR 0137).
        provider_ca_pem: PEM certificate(s) that sign the provider's TLS certificate, when they are
            not in the engine's trust store (a private CA); delivered to the proxy with its config.
        redirect_hosts: With a directory, the hosts the service is reached on; the broker only
            redirects back to ``https://<host><auth_path>/callback`` for these.
        client_id: The client registered for Switchyard at the provider.
        client_secret_ref: OpenBao path of the client secret (never the secret).
        cookie_secret_ref: OpenBao path of the key that signs and encrypts session cookies
            (16, 24 or 32 bytes). One key per access set; rotate it to end every session.
        scopes: Scopes to request; ``openid`` is required.
        groups_claim: Claim that carries the user's groups.
        cookie_name: Session cookie name.
        cookie_expire_s: Absolute session lifetime.
        cookie_refresh_s: How often the session is refreshed against the provider (a user whose
            account was disabled loses the session at the next refresh). Less than the lifetime.
    """

    issuer: Issuer | None = None
    directory_id: UUID | None = None
    redirect_hosts: tuple[Hostname, ...] = Field(default=(), max_length=16)
    provider_ca_pem: CertPem | None = None
    client_id: ClientId
    client_secret_ref: SecretRef
    cookie_secret_ref: SecretRef
    scopes: tuple[Annotated[str, Field(pattern=r"^[A-Za-z0-9._:/-]{1,64}$")], ...] = (
        "openid",
        "email",
        "profile",
    )
    groups_claim: ClaimName = "groups"
    cookie_name: CookieName = "_sy_auth"
    cookie_expire_s: int = Field(default=28_800, ge=300, le=604_800)
    cookie_refresh_s: int = Field(default=900, ge=60, le=86_400)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.issuer is None) == (self.directory_id is None):
            raise ValueError("set exactly one of issuer and directory_id")
        if self.directory_id is not None and not self.redirect_hosts:
            raise ValueError(
                "a directory login needs redirect_hosts (the hosts users reach the service on)"
            )
        if self.directory_id is None and self.redirect_hosts:
            raise ValueError("redirect_hosts only goes with directory_id")
        if "openid" not in self.scopes:
            raise ValueError("scopes must include openid")
        if len(set(self.scopes)) != len(self.scopes) or len(self.scopes) > 10:
            raise ValueError("scopes must be unique and at most 10")
        if self.cookie_refresh_s >= self.cookie_expire_s:
            raise ValueError("cookie_refresh_s must be less than cookie_expire_s")
        return self


class ClaimCheck(Model):
    """A condition on a claim of the signed-in user's ID token (ADR 0138).

    ``op`` is ``present``, ``true`` (a JSON ``true``), ``equals`` (``value``) or ``in``
    (``values``). The token was verified by the authentication proxy.
    """

    name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    op: Literal["present", "true", "equals", "in"] = "true"
    value: HeaderValue | None = None
    values: tuple[HeaderValue, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.op == "equals") != (self.value is not None):
            raise ValueError("value goes with op equals, and only with it")
        if (self.op == "in") != bool(self.values):
            raise ValueError("values go with op in, and only with it")
        return self


class Posture(Model):
    """Device conditions a request must meet besides the login (ADR 0138).

    Attributes:
        device_certificate: The connection presented a client certificate the port's client CA
            accepted (the service needs a port with ``client_ca_id``).
        claims: Conditions on ID-token claims, all of which must hold (for example a
            ``device_compliant`` claim the identity provider issues).
    """

    device_certificate: bool = False
    claims: tuple[ClaimCheck, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def _something(self) -> Self:
        if not self.device_certificate and not self.claims:
            raise ValueError("posture needs device_certificate or claims")
        return self


class AccessRule(Model):
    """What a request needs before it reaches the origin.

    Attributes:
        match: Requests the rule covers; the first rule that matches decides.
        mode: ``public`` (no login), ``login`` (any authenticated user), ``groups`` (a member of
            at least one of ``groups``) or ``email_domains`` (an address in one of the domains).
        posture: Device conditions (ADR 0138).
        min_factors: The person must have passed at least this many factors of the directory's
            login flow (ADR 0195); a session with fewer is sent to complete the next one.
    """

    match: RouteMatch = RouteMatch()
    mode: Literal["public", "login", "groups", "email_domains"] = "login"
    groups: tuple[GroupName, ...] = Field(default=(), max_length=32)
    email_domains: tuple[Hostname, ...] = Field(default=(), max_length=32)
    posture: Posture | None = None
    min_factors: int | None = Field(default=None, ge=2, le=4)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.posture is not None and self.mode == "public":
            raise ValueError("posture does not go with mode public")
        if self.min_factors is not None and self.mode == "public":
            raise ValueError("min_factors does not go with mode public")
        if self.mode == "groups" and not self.groups:
            raise ValueError("mode groups needs groups")
        if self.mode == "email_domains" and not self.email_domains:
            raise ValueError("mode email_domains needs email_domains")
        if self.mode != "groups" and self.groups:
            raise ValueError("groups only goes with mode groups")
        if self.mode != "email_domains" and self.email_domains:
            raise ValueError("email_domains only goes with mode email_domains")
        return self


class IdentityHeaders(Model):
    """Headers that carry the signed-in user to the origin (ADR 0132).

    A name set to ``None`` turns that header off. The names are removed from every client
    request before the user's own values are written, so an origin can trust them when the
    service is only reachable through the engine.
    """

    user: HeaderName | None = "X-Auth-User"
    email: HeaderName | None = "X-Auth-Email"
    groups: HeaderName | None = "X-Auth-Groups"
    token: HeaderName | None = None

    @model_validator(mode="after")
    def _distinct_and_allowed(self) -> Self:
        names = [n.lower() for n in (self.user, self.email, self.groups, self.token) if n]
        if len(set(names)) != len(names):
            raise ValueError("identity header names must be different")
        reserved = v.FORBIDDEN_HEADERS | v.MANAGED_REQUEST_HEADERS | v.AUTH_INTERNAL_HEADERS
        for name in names:
            if name in reserved:
                raise ValueError(f"header {name} cannot carry identity")
        return self


class AccessPolicySet(TenantOwned):
    """Require a login (and optionally a group or e-mail domain) for a service (ADR 0130 to 0132).

    The engine runs an authentication proxy for the set; requests that need a login are sent
    through it and come back to the same frontend once authenticated.

    Attributes:
        oidc: The identity provider settings.
        default_mode: What requests that match no rule need: ``login`` (the default, closed) or
            ``public``.
        rules: Ordered rules; the first match decides.
        identity_headers: Which headers carry the user to the origin.
        auth_path: Path prefix of the login endpoints (``/oauth2/start``, ``/oauth2/callback``,
            ``/oauth2/sign_out``), reserved on every host of the service.
    """

    kind: Literal["access"] = "access"
    oidc: OidcSettings
    default_mode: Literal["login", "public"] = "login"
    rules: tuple[AccessRule, ...] = Field(default=(), max_length=32)
    identity_headers: IdentityHeaders = IdentityHeaders()
    auth_path: UrlPath = "/oauth2"

    @model_validator(mode="after")
    def _path(self) -> Self:
        if len(self.auth_path) < 2 or self.auth_path.endswith("/"):
            raise ValueError("auth_path must be a directory such as /oauth2")
        return self


JwtAlgorithm = Literal[
    "RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"
]


class JwtKey(Model):
    """A public key that signs tokens (ADR 0135); the engine never holds a private or shared key.

    Attributes:
        kid: The ``kid`` header the key belongs to; without it the key is tried for every token.
        alg: The signature algorithm the token must declare.
        public_key_pem: The key as a PEM ``PUBLIC KEY`` block (convert a JWK with
            ``syctl jwks convert``).
    """

    kid: Annotated[str, Field(pattern=r"^[A-Za-z0-9._:/@=-]{1,128}$")] | None = None
    alg: JwtAlgorithm = "RS256"
    public_key_pem: PublicKeyPem


class JwtRule(Model):
    """Require a valid bearer token on the requests ``match`` selects.

    A token is valid when one of ``keys`` verifies its signature, ``iss`` equals ``issuer``, ``aud``
    contains one of ``audiences`` (when given), ``exp`` is in the future (allowing ``leeway_s``) and
    every ``required_scopes`` entry is in the space-separated ``scope`` claim. Anything else is
    answered 401.

    Attributes:
        match: Requests the rule covers (empty: all).
        issuer: The expected ``iss`` claim.
        audiences: Accepted ``aud`` values (a string claim or an element of an array claim).
        required_scopes: Scopes the token must carry.
        leeway_s: Clock difference tolerated on ``exp``.
        keys: The issuer's public keys (up to 8; several keep tokens valid across a key
            rotation). With ``jwks_url`` the controller keeps them in step with the issuer.
        jwks_url: Where the issuer publishes its JWKS (``https``). The controller fetches it
            every few minutes and replaces ``keys`` when they changed (ADR 0153). Until the
            first fetch has been applied the rule has no keys and answers 401.
        subject_header: Request header that receives the ``sub`` claim of a valid token;
            client copies of it are removed first.
    """

    match: RouteMatch = RouteMatch()
    issuer: Issuer
    audiences: tuple[Annotated[str, Field(pattern=r"^[A-Za-z0-9._~:/@=+-]{1,256}$")], ...] = Field(
        default=(), max_length=8
    )
    required_scopes: tuple[Annotated[str, Field(pattern=r"^[A-Za-z0-9._:/-]{1,64}$")], ...] = Field(
        default=(), max_length=8
    )
    leeway_s: int = Field(default=30, ge=0, le=300)
    keys: tuple[JwtKey, ...] = Field(default=(), max_length=8)
    jwks_url: JwksUrl | None = None
    subject_header: HeaderName | None = None

    @model_validator(mode="after")
    def _needs_keys(self) -> Self:
        if not self.keys and self.jwks_url is None:
            raise ValueError("a rule needs keys or a jwks_url")
        return self

    @model_validator(mode="after")
    def _header(self) -> Self:
        if self.subject_header is not None:
            name = self.subject_header.lower()
            if name in v.FORBIDDEN_HEADERS | v.MANAGED_REQUEST_HEADERS | v.AUTH_INTERNAL_HEADERS:
                raise ValueError(f"header {self.subject_header} cannot carry the subject")
        return self


class JwtPolicySet(TenantOwned):
    """Validate JWT bearer tokens for APIs, with no login page (ADR 0135)."""

    kind: Literal["jwt"] = "jwt"
    rules: tuple[JwtRule, ...] = Field(min_length=1, max_length=16)


class CertIdentity(Model):
    """Map a client certificate to a user and groups (ADR 0136).

    Attributes:
        by: ``cn`` (the subject common name) or ``sha1`` (the certificate's SHA-1 fingerprint, hex,
            for individual certificates).
        value: The common name, or 40 hex digits.
        user: The user name written to the user header.
        groups: Groups written to the groups header.
    """

    by: Literal["cn", "sha1"] = "cn"
    value: CertName
    user: CertName
    groups: tuple[GroupName, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _sha1(self) -> Self:
        if self.by == "sha1" and not re.fullmatch(r"[0-9a-f]{40}", self.value):
            raise ValueError("a sha1 value is 40 lower-case hex digits")
        return self


class ClientCertRule(Model):
    """What a request needs from its client certificate.

    Attributes:
        match: Requests the rule covers (empty: all). The first rule that matches decides.
        mode: ``valid`` (any certificate the port's client CA accepts), ``identity`` (the
            certificate must be in ``identities``) or ``groups`` (and its identity must have one of
            ``groups``).
        groups: Groups for mode ``groups``.
    """

    match: RouteMatch = RouteMatch()
    mode: Literal["valid", "identity", "groups"] = "valid"
    groups: tuple[GroupName, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _groups(self) -> Self:
        if (self.mode == "groups") != bool(self.groups):
            raise ValueError("groups go with mode groups, and only with it")
        return self


class ClientCertPolicySet(TenantOwned):
    """Use the TLS client certificate as the identity (ADR 0136).

    Attributes:
        identities: Certificates the set knows (up to 500).
        rules: Ordered rules; the first that matches decides. Requests that match no rule are
            not checked.
        user_header: Header that receives the mapped user (client copies removed first).
        groups_header: Header that receives the mapped groups, comma separated.
    """

    kind: Literal["client_cert"] = "client_cert"
    identities: tuple[CertIdentity, ...] = Field(default=(), max_length=500)
    rules: tuple[ClientCertRule, ...] = Field(min_length=1, max_length=32)
    user_header: HeaderName | None = "X-Cert-User"
    groups_header: HeaderName | None = "X-Cert-Groups"

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        keys = [(i.by, i.value) for i in self.identities]
        if len(set(keys)) != len(keys):
            raise ValueError("identities must be unique")
        names = [h.lower() for h in (self.user_header, self.groups_header) if h]
        if len(set(names)) != len(names):
            raise ValueError("user_header and groups_header must differ")
        reserved = v.FORBIDDEN_HEADERS | v.MANAGED_REQUEST_HEADERS | v.AUTH_INTERNAL_HEADERS
        if any(n in reserved for n in names):
            raise ValueError("those headers cannot carry identity")
        needs_identity = any(r.mode != "valid" for r in self.rules)
        if needs_identity and not self.identities:
            raise ValueError("rules with mode identity or groups need identities")
        return self


#: User-agent substrings of well-known scanners and exploit tools (ADR 0170), lower case.
KNOWN_SCANNERS = (
    "acunetix",
    "dirbuster",
    "gobuster",
    "havij",
    "masscan",
    "netsparker",
    "nikto",
    "nmap",
    "nessus",
    "nuclei",
    "openvas",
    "sqlmap",
    "wpscan",
    "zgrab",
)
UserAgentToken = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._/+-]{1,63}$")]


class JsChallenge(Model):
    """Make a new client prove it runs a browser before it gets any answer (ADR 0171).

    The page asks the browser to find a number whose SHA-256 (with the client address) starts
    with ``difficulty`` zero hex digits: a fraction of a second of work for a person and a cost
    per client for a bot farm. The result is kept in a cookie for ``valid_for_s``. Nothing secret
    is involved.

    Attributes:
        difficulty: Leading zero hex digits required; 3 is about 4,000 hashes, 4 about 65,000,
            5 about a million.
        valid_for_s: How long a solved challenge lasts.
        skip_paths: Paths that are never challenged (``path_beg``), such as an API.
        skip_user_agents: Lower-case ``User-Agent`` substrings that are never challenged (search
            engines). The header is easy to fake; use it only where that risk is acceptable.

    Example:
        >>> JsChallenge().difficulty
        4
    """

    difficulty: int = Field(default=4, ge=3, le=5)
    valid_for_s: int = Field(default=3600, ge=300, le=86_400)
    skip_paths: tuple[UrlPath, ...] = Field(default=(), max_length=16)
    skip_user_agents: tuple[UserAgentToken, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.skip_paths)) != len(self.skip_paths):
            raise ValueError("skip_paths must be unique")
        if len(set(self.skip_user_agents)) != len(self.skip_user_agents):
            raise ValueError("skip_user_agents must be unique")
        return self


class CaptchaGate(Model):
    """Ask new clients to pass a CAPTCHA from a provider before they get any answer (ADR 0172).

    The page shows the provider's widget; a Lua service on the engine asks the provider to verify
    the answer, then sets a signed cookie. The provider's secret key is kept in OpenBao.

    Attributes:
        provider: ``turnstile`` (Cloudflare) or ``hcaptcha``.
        site_key: The public site key of the widget.
        secret_ref: OpenBao path of the provider's secret key (never the key itself).
        valid_for_s: How long a passed CAPTCHA lasts.
        skip_paths: Paths that are never gated (``path_beg``), such as an API.
        skip_user_agents: Lower-case ``User-Agent`` substrings that are never gated.

    Example:
        >>> CaptchaGate(provider="hcaptcha", site_key="10000000-ffff-ffff-ffff-000000000001",
        ...             secret_ref="tenants/acme/captcha").valid_for_s
        3600
    """

    provider: Literal["turnstile", "hcaptcha"]
    site_key: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,128}$")]
    secret_ref: SecretRef
    valid_for_s: int = Field(default=3600, ge=300, le=86_400)
    skip_paths: tuple[UrlPath, ...] = Field(default=(), max_length=16)
    skip_user_agents: tuple[UserAgentToken, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.skip_paths)) != len(self.skip_paths):
            raise ValueError("skip_paths must be unique")
        if len(set(self.skip_user_agents)) != len(self.skip_user_agents):
            raise ValueError("skip_user_agents must be unique")
        return self


class ProtectionPolicySet(TenantOwned):
    """Bot and DDoS protection for a service (ADR 0170).

    Per-source connection limits, slow-request timeouts and user-agent rules. Request-rate limits
    stay in ``rate_limit`` sets.

    Attributes:
        max_connections_per_source: Connections one address may hold open; more are refused.
        max_new_connections_per_source: New connections one address may open per
            ``connection_rate_period_s``; more are refused.
        connection_rate_period_s: Window of the connection rate.
        header_timeout_s: How long a client has to send the whole request head (``timeout
            http-request``); the answer to slow-header attacks.
        client_timeout_s: Idle time allowed on a client connection (``timeout client``).
        block_empty_user_agent: Refuse requests with a missing or empty ``User-Agent``.
        block_scanners: Refuse the user agents of well-known scanners (``KNOWN_SCANNERS``).
        deny_user_agents: More lower-case substrings to refuse in the ``User-Agent``.
        bot_action: ``deny`` answers 403; ``silent_drop`` closes the connection without an answer.
        exempt_cidrs: Sources that skip every rule of the set (monitoring, trusted partners).
        challenge: A JavaScript proof-of-work challenge for new clients (ADR 0171).
        captcha: A CAPTCHA from a provider instead (ADR 0172); not together with ``challenge``.
        table_size: Addresses the connection table holds.

    Example:
        >>> ProtectionPolicySet.model_validate(
        ...     {"id": "00000000-0000-0000-0000-000000000001",
        ...      "tenant_id": "00000000-0000-0000-0000-000000000002", "name": "p",
        ...      "block_scanners": True}
        ... ).bot_action
        'deny'
    """

    kind: Literal["protection"] = "protection"
    max_connections_per_source: int | None = Field(default=None, ge=1, le=100_000)
    max_new_connections_per_source: int | None = Field(default=None, ge=1, le=100_000)
    connection_rate_period_s: Literal[1, 10, 60] = 10
    header_timeout_s: int | None = Field(default=None, ge=1, le=60)
    client_timeout_s: int | None = Field(default=None, ge=1, le=3600)
    block_empty_user_agent: bool = False
    block_scanners: bool = False
    deny_user_agents: tuple[UserAgentToken, ...] = Field(default=(), max_length=32)
    bot_action: Literal["deny", "silent_drop"] = "deny"
    exempt_cidrs: tuple[IPNetwork, ...] = Field(default=(), max_length=32)
    challenge: JsChallenge | None = None
    captcha: CaptchaGate | None = None
    table_size: int = Field(default=100_000, ge=1_000, le=1_000_000)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if len(set(self.deny_user_agents)) != len(self.deny_user_agents):
            raise ValueError("deny_user_agents must be unique")
        if self.challenge is not None and self.captcha is not None:
            raise ValueError("use either challenge or captcha, not both")
        if not (
            self.max_connections_per_source
            or self.max_new_connections_per_source
            or self.header_timeout_s
            or self.client_timeout_s
            or self.block_empty_user_agent
            or self.block_scanners
            or self.deny_user_agents
            or self.challenge
            or self.captcha
        ):
            raise ValueError("a protection set needs at least one protection")
        return self

    @property
    def tracks_connections(self) -> bool:
        """True when the set needs a per-source connection table."""
        return bool(self.max_connections_per_source or self.max_new_connections_per_source)


class LdapConnector(Model):
    """Sign in against an LDAP or Active Directory server (ADR 0137).

    Attributes:
        host: Server name; ``port`` defaults to 636 (``ldaps``). With ``starttls`` use 389.
        security: ``ldaps`` or ``starttls``; a clear-text connection is not offered.
        root_ca_pem: PEM certificate(s) that sign the server certificate (public; omit to use the
            broker's system roots).
        bind_dn: Service account used to search.
        bind_password_ref: OpenBao path of its password.
        user_base_dn / user_filter: Where and how users are found; the sign-in name is matched
            with ``username_attr``.
        group_base_dn: Where groups are searched (omit for no groups); a user's groups are those
            whose ``group_member_attr`` holds the user's ``group_user_attr`` (``DN`` or an
            attribute name).
    """

    type: Literal["ldap"] = "ldap"
    host: Hostname
    port: Port = 636
    security: Literal["ldaps", "starttls"] = "ldaps"
    root_ca_pem: CertPem | None = None
    bind_dn: Dn
    bind_password_ref: SecretRef
    user_base_dn: Dn
    user_filter: LdapFilter = "(objectClass=person)"
    username_attr: LdapAttr = "uid"
    id_attr: LdapAttr = "uid"
    email_attr: LdapAttr = "mail"
    name_attr: LdapAttr = "cn"
    group_base_dn: Dn | None = None
    group_filter: LdapFilter = "(objectClass=groupOfNames)"
    group_user_attr: LdapAttr = "DN"
    group_member_attr: LdapAttr = "member"
    group_name_attr: LdapAttr = "cn"


class SamlConnector(Model):
    """Sign in through a SAML 2.0 identity provider (ADR 0137).

    Attributes:
        sso_url: The provider's single sign-on URL (https).
        ca_pem: The provider's signing certificate (public PEM); assertions must be signed by it.
        username_attr / email_attr / groups_attr: Assertion attributes to read (``groups_attr`` of
            ``None`` means no groups).
    """

    type: Literal["saml"] = "saml"
    sso_url: Issuer
    ca_pem: CertPem
    username_attr: ClaimName = "name"
    email_attr: ClaimName = "email"
    groups_attr: ClaimName | None = "groups"


class LocalUser(Model):
    """A user kept by Switchyard: a name, an address and a bcrypt hash in OpenBao."""

    username: CertName
    email: EmailAddress
    password_hash_ref: SecretRef


class LocalConnector(Model):
    """Users defined here, with passwords checked by the broker. They have no groups."""

    type: Literal["local"] = "local"
    users: tuple[LocalUser, ...] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [u.username for u in self.users]
        emails = [u.email.lower() for u in self.users]
        if len(set(names)) != len(names) or len(set(emails)) != len(emails):
            raise ValueError("user names and addresses must be unique")
        return self


#: Groups the login gate adds to a person who passed N factors (``sy-factors-1``, ``sy-factors-2``,
#: ...). Groups with this prefix that a RADIUS server returns are dropped (ADR 0195).
FACTOR_GROUP_PREFIX = "sy-factors-"


class RadiusServerSpec(Model):
    """One RADIUS server a ``radius`` directory can ask (ADR 0195).

    Attributes:
        name: Name a flow step uses to pick this server.
        host / port: Where the server listens (UDP, 1812 by default).
        secret_ref: OpenBao path of the shared secret (never the secret).
        timeout_s / retries: Per-attempt wait and number of retransmissions.
    """

    name: Name
    host: Hostname
    port: Port = 1812
    secret_ref: SecretRef
    timeout_s: int = Field(default=3, ge=1, le=10)
    retries: int = Field(default=2, ge=0, le=5)


class FlowCondition(Model):
    """When a step applies (ADR 0195); everything given must hold.

    Attributes:
        groups_any: The person has at least one of these groups from the steps already passed.
        source_in: The client address is inside one of these networks.
        source_not_in: The client address is outside all of these networks.
    """

    groups_any: tuple[GroupName, ...] = Field(default=(), max_length=16)
    source_in: tuple[IPNetwork, ...] = Field(default=(), max_length=16)
    source_not_in: tuple[IPNetwork, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def _something(self) -> Self:
        if not (self.groups_any or self.source_in or self.source_not_in):
            raise ValueError("a condition needs groups_any, source_in or source_not_in")
        for group in self.groups_any:
            if group.startswith(FACTOR_GROUP_PREFIX):
                raise ValueError(f"groups starting with {FACTOR_GROUP_PREFIX} are reserved")
        return self


class FlowStep(Model):
    """One factor of a login flow (ADR 0195).

    Attributes:
        name: Step name (unique in the flow).
        kind: ``password`` asks for user name and password; ``code`` asks for one more secret
            (a one-time code) for the user already named.
        server: The RADIUS server that checks this step (default: the first one).
        prompt: Text above the field; plain text.
        when: Conditions under which the step is asked; the first step is always asked.
    """

    name: Name
    kind: Literal["password", "code"] = "password"
    server: Name | None = None
    prompt: Annotated[str, Field(pattern=r"^[A-Za-z0-9 .,:;!?()'_/-]{1,80}$")] = "Password"
    when: FlowCondition | None = None


class LoginFlow(Model):
    """Ordered factors with conditions, in the manner of Citrix nFactor (ADR 0195).

    The steps run in order; a step whose ``when`` does not hold is skipped. After every passed
    step the person holds ``sy-factors-<n>`` for n factors so far, so access rules can ask for a
    stronger login with ``min_factors``.
    """

    steps: tuple[FlowStep, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        names = [s.name for s in self.steps]
        if len(set(names)) != len(names):
            raise ValueError("step names must be unique")
        if self.steps[0].kind != "password" or self.steps[0].when is not None:
            raise ValueError("the first step must be a password step without a condition")
        return self


class RadiusConnector(Model):
    """Sign in against RADIUS servers through the login gate (ADR 0195).

    Attributes:
        servers: Up to four servers (for instance a password server and an OTP server).
        nas_identifier: Sent as NAS-Identifier.
        group_attribute: Where the server returns groups: ``filter_id`` or ``class``.
        email_domain: Users get the address ``<name>@<email_domain>`` (RADIUS has none).
        signing_secret_ref: OpenBao path of the key that signs the gate's short-lived cookies.
        login_timeout_s: How long a person has to finish all steps.
    """

    type: Literal["radius"] = "radius"
    servers: tuple[RadiusServerSpec, ...] = Field(min_length=1, max_length=4)
    nas_identifier: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")] = "switchyard"
    group_attribute: Literal["filter_id", "class"] = "filter_id"
    email_domain: Hostname
    signing_secret_ref: SecretRef
    login_timeout_s: int = Field(default=300, ge=60, le=1800)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        names = [s.name for s in self.servers]
        if len(set(names)) != len(names):
            raise ValueError("server names must be unique")
        return self


class DirectoryPolicySet(TenantOwned):
    """A place users sign in that is not an OIDC provider of their own: LDAP, SAML or local users.

    A central broker (Dex, one instance per directory) turns it into an OIDC issuer that an
    access set names with ``oidc.directory_id`` (ADR 0137). Directories are not attached to
    services.
    """

    kind: Literal["directory"] = "directory"
    connector: Annotated[
        LdapConnector | SamlConnector | LocalConnector | RadiusConnector,
        Field(discriminator="type"),
    ]
    flow: LoginFlow | None = None

    @model_validator(mode="after")
    def _flow_matches(self) -> Self:
        if self.flow is None:
            return self
        if not isinstance(self.connector, RadiusConnector):
            raise ValueError("a login flow goes with a radius connector")
        servers = {srv.name for srv in self.connector.servers}
        for step in self.flow.steps:
            if step.server is not None and step.server not in servers:
                raise ValueError(f"step {step.name} names an unknown server {step.server}")
        return self


PolicySet = Annotated[
    RedirectPolicySet
    | AllowListPolicySet
    | RewritePolicySet
    | RespondPolicySet
    | ErrorPagesPolicySet
    | RateLimitPolicySet
    | WafPolicySet
    | ProtectionPolicySet
    | AccessPolicySet
    | JwtPolicySet
    | ClientCertPolicySet
    | DirectoryPolicySet,
    Field(discriminator="kind"),
]
#: Policy-set classes a virtual service may list in ``policy_set_ids`` (not the allow list).
ATTACHABLE_POLICY_KINDS = (
    RedirectPolicySet,
    RewritePolicySet,
    RespondPolicySet,
    ErrorPagesPolicySet,
    RateLimitPolicySet,
    WafPolicySet,
    ProtectionPolicySet,
    AccessPolicySet,
    JwtPolicySet,
    ClientCertPolicySet,
)
