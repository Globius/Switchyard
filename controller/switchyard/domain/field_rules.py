"""Which fields a virtual service, its ports and its pools may use, by protocol.

The snapshot validator enforces these rules (``domain.snapshot``) and the API publishes them
(``GET /api/v1/schema/virtual-service``) so a form can disable the fields a protocol cannot use.
Both read the same table, so they cannot drift apart. See ADR 0020.
"""

from dataclasses import dataclass

from switchyard.domain.models import Algorithm, MonitorType, Protocol

#: Fields of a virtual service that a UDP service cannot use.
UDP_FORBIDDEN_SERVICE_FIELDS = (
    "certificate_ids",
    "policy_set_ids",
    "allow_list_id",
    "enforce_allow_list",
    "deny_list_id",
    "restore_client_ip_header",
    "routes",
    "compression",
    "cache",
)
#: Fields of a port that a UDP service cannot use.
UDP_FORBIDDEN_PORT_FIELDS = (
    "tls",
    "proxy_protocol",
    "http2",
    "https_redirect_port",
    "tls_profile",
    "client_ca_id",
    "tls_passthrough",
    "http3",
)


@dataclass(frozen=True)
class ProtocolRules:
    """The field rules of one protocol.

    Attributes:
        protocol: The service protocol.
        service_forbidden: Virtual service fields that are rejected.
        port_forbidden: Port fields that are rejected.
        pool_forbidden: Pool fields that are rejected on pools the service uses.
        algorithms: Pool algorithms that are allowed.
        monitor_types: Health monitor types that are allowed on pools the service uses.
        member_port_required: Whether every member of the pool needs its own port.
    """

    protocol: Protocol
    service_forbidden: tuple[str, ...]
    port_forbidden: tuple[str, ...]
    pool_forbidden: tuple[str, ...]
    algorithms: tuple[Algorithm, ...]
    monitor_types: tuple[MonitorType, ...]
    member_port_required: bool


def protocol_rules(protocol: Protocol) -> ProtocolRules:
    """Return the field rules of ``protocol``.

    Example:
        >>> protocol_rules(Protocol.UDP).monitor_types
        (<MonitorType.UDP: 'udp'>,)
        >>> protocol_rules(Protocol.HTTP).service_forbidden
        ()
    """
    if protocol is Protocol.UDP:
        return ProtocolRules(
            protocol,
            UDP_FORBIDDEN_SERVICE_FIELDS,
            UDP_FORBIDDEN_PORT_FIELDS,
            (
                "tls",
                "tls_verify",
                "persistence",
                "passive_check",
                "slow_start_s",
                "send_proxy",
                "client_certificate_id",
            ),
            tuple(a for a in Algorithm if a is not Algorithm.RANDOM),
            (MonitorType.UDP,),
            True,
        )
    forbidden = (
        "policy_set_ids",
        "restore_client_ip_header",
        "compression",
    )
    return ProtocolRules(
        protocol,
        () if protocol is Protocol.HTTP else forbidden,
        ("tls_passthrough",)
        if protocol is Protocol.HTTP
        else ("http2", "http3", "https_redirect_port"),
        (),
        tuple(Algorithm),
        (MonitorType.TCP, MonitorType.HTTP),
        False,
    )
