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
SecretRef = Annotated[str, AfterValidator(v.validate_secret_ref)]


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
    """

    ha_mode: HaMode
    networks: tuple[SeGroupNetwork, ...] = Field(min_length=1)
    maxconn: int = Field(default=20_000, ge=1, le=10_000_000)
    canary_seconds: int = Field(default=30, ge=0, le=600)
    routing_profile_id: UUID | None = None


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
    """

    peer_ip: IPAddress
    remote_asn: int = Field(ge=1, le=4_294_967_295)
    md5_ref: SecretRef | None = None


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
        max_paths: ECMP width.
        static_routes: Routes installed on every engine.
    """

    kind: Literal["bgp"] = "bgp"
    asn: int = Field(ge=1, le=4_294_967_295)
    bfd: bool = True
    peers: tuple[BgpPeer, ...] = Field(min_length=1)
    max_paths: int = Field(default=4, ge=1, le=64)
    static_routes: tuple[StaticRoute, ...] = ()


RoutingProfile = Annotated[VrrpProfile | BgpProfile, Field(discriminator="kind")]


class RedirectAction(Model):
    """An HTTP redirect: absolute path or URL, with a status code."""

    location: Location
    code: RedirectCode = 301


class RouteMatch(Model):
    """Request match: an optional host (exact or ``*.`` wildcard) and an optional path."""

    host: HostPattern | None = None
    path_prefix: UrlPath | None = None
    path_exact: UrlPath | None = None

    @model_validator(mode="after")
    def _one_path(self) -> Self:
        if self.path_prefix is not None and self.path_exact is not None:
            raise ValueError("use either path_prefix or path_exact, not both")
        return self

    @property
    def is_empty(self) -> bool:
        """True when the match has no host and no path (matches every request)."""
        return self.host is None and self.path_prefix is None and self.path_exact is None


class VsRoute(Model):
    """Route a request to a pool or answer it with a redirect.

    Attributes:
        order: Position among path routes; lower runs first. Host-only routes are looked up in map
            files and are order-independent (exact host wins over wildcard).
        match: What the request must match. Must not be empty.
        pool_id: Target pool, or ``None`` when ``redirect`` is set.
        redirect: Redirect to answer with, or ``None`` when ``pool_id`` is set.
    """

    order: int = Field(default=0, ge=0)
    match: RouteMatch
    pool_id: UUID | None = None
    redirect: RedirectAction | None = None

    @model_validator(mode="after")
    def _one_action(self) -> Self:
        if (self.pool_id is None) == (self.redirect is None):
            raise ValueError("a route needs exactly one of pool_id or redirect")
        if self.match.is_empty:
            raise ValueError("a route must match a host or a path; use default_pool_id as fallback")
        return self


class VsPort(Model):
    """One listening port on a virtual service's VIP.

    Attributes:
        port: Listening port.
        tls: Terminate TLS on this port using the service's certificates.
        http2: Offer HTTP/2 via ALPN (requires ``tls``).
        proxy_protocol: Accept the PROXY protocol header from the client side.
        https_redirect_port: On a plaintext port, redirect every request to ``https`` on this port
            (which must be a TLS port of the same service).
    """

    port: Port
    tls: bool = False
    http2: bool = False
    proxy_protocol: bool = False
    https_redirect_port: Port | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.http2 and not self.tls:
            raise ValueError("http2 requires tls")
        if self.https_redirect_port is not None and self.tls:
            raise ValueError("https_redirect_port is only valid on a plaintext port")
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
        policy_set_ids: Redirect policy sets, applied before routing, in order.
        allow_list_id: Allow-list policy set (for example the Cloudflare ranges).
        enforce_allow_list: Reject connections from sources outside the allow list.
        restore_client_ip_header: Header (for example ``CF-Connecting-IP``) that carries the real
            client address; honoured only when the connection source is in the allow list.
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
        if any(p.tls for p in self.ports) and not self.certificate_ids:
            raise ValueError("a TLS port needs at least one certificate")
        if self.protocol is Protocol.HTTP:
            if not self.routes and self.default_pool_id is None:
                raise ValueError("an http service needs routes or default_pool_id")
        else:
            if self.routes or self.policy_set_ids:
                raise ValueError(f"a {self.protocol} service cannot have routes or policy sets")
            if self.default_pool_id is None:
                raise ValueError(f"a {self.protocol} service needs default_pool_id")
            if any(p.http2 or p.https_redirect_port is not None for p in self.ports):
                raise ValueError("http2 and https_redirect_port are only valid for http services")
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


class PoolMember(Model):
    """One backend server.

    Attributes:
        id: Stable id, used by the runtime enable/disable/drain endpoint.
        address: Backend IP address.
        port: Backend port, or ``None`` to reuse the port the client connected to.
        weight: 0-256; 0 takes no new connections.
        admin_state: Operator state; ``disabled`` and ``drain`` take no new connections.
        name: HAProxy server name; derived from address and port when omitted.
    """

    id: UUID
    address: IPAddress
    port: Port | None = None
    weight: int = Field(default=100, ge=0, le=256)
    admin_state: AdminState = AdminState.ENABLED
    name: Name | None = None

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


class Pool(TenantOwned):
    """A group of backend servers.

    Attributes:
        algorithm: Load-balancing algorithm.
        persistence: Optional persistence profile.
        health_monitor_id: Optional shared health monitor.
        tls: Connect to members over TLS.
        tls_verify: Verify the member certificate against the system CA store. Off by default to
            match Avi behaviour for internal backends with private certificates.
        members: Backend servers, unique by name.
    """

    algorithm: Algorithm = Algorithm.ROUNDROBIN
    persistence: PersistenceSourceIp | None = None
    health_monitor_id: UUID | None = None
    tls: bool = False
    tls_verify: bool = False
    members: tuple[PoolMember, ...] = ()

    @model_validator(mode="after")
    def _members(self) -> Self:
        names = [m.server_name for m in self.members]
        if len(set(names)) != len(names):
            raise ValueError("pool member names must be unique")
        if self.tls_verify and not self.tls:
            raise ValueError("tls_verify requires tls")
        return self


class HttpCheck(Model):
    """HTTP health check request and expectation."""

    method: Literal["GET", "HEAD", "OPTIONS"] = "GET"
    path: UrlPath = "/"
    host: Hostname | None = None
    expect_status: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _codes(self) -> Self:
        if any(not 100 <= c <= 599 for c in self.expect_status):
            raise ValueError("expect_status codes must be between 100 and 599")
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
    """

    type: MonitorType
    port: Port | None = None
    interval_s: int = Field(default=5, ge=1, le=300)
    timeout_s: int = Field(default=3, ge=1, le=300)
    rise: int = Field(default=2, ge=1, le=100)
    fall: int = Field(default=3, ge=1, le=100)
    http: HttpCheck | None = None
    udp: UdpCheck | None = None

    @model_validator(mode="after")
    def _http(self) -> Self:
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
    """

    source: CertSource
    sans: tuple[HostPattern, ...] = Field(min_length=1)
    not_after: datetime
    secret_ref: SecretRef
    renew_policy: RenewPolicy = RenewPolicy.MANUAL

    @model_validator(mode="after")
    def _checked(self) -> Self:
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


PolicySet = Annotated[RedirectPolicySet | AllowListPolicySet, Field(discriminator="kind")]
