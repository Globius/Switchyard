# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Request and response models of the GSLB API (ADR 0160)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from switchyard.api.schemas import ApiModel


class GslbZoneIn(ApiModel):
    """A zone to host for a tenant."""

    name: str = Field(description="The zone, for example `gslb.example.com`.")
    nameservers: list[str] = Field(
        description="Host names of your PowerDNS servers, as they appear in the zone's NS records."
    )


class GslbZoneRead(ApiModel):
    """A zone."""

    id: UUID
    name: str
    nameservers: list[str]
    dnssec: bool
    version: int
    created_by: str
    created_at: datetime


class GslbDsRead(ApiModel):
    """DNSSEC state of a zone."""

    zone: str
    dnssec: bool
    ds: list[str] = Field(description="DS records to publish at the zone's parent.")


class WideIpHealth(ApiModel):
    """How PowerDNS decides whether a member is up (it checks from the DNS servers)."""

    kind: Literal["tcp", "http"] = "tcp"
    port: int = Field(default=443, ge=1, le=65535)
    scheme: Literal["http", "https"] | None = Field(
        default=None,
        description="`http` checks only; default `http` for port 80, `https` otherwise.",
    )
    path: str | None = Field(default=None, description="`http` checks only; starts with `/`.")
    match: str | None = Field(default=None, description="Text the response must contain.")
    host: str | None = Field(
        default=None, description="Host name to ask for (default: the wide IP)."
    )
    timeout_ms: int = Field(
        default=1000, ge=100, le=5000, description="How long PowerDNS waits for one check."
    )


class WideIpMember(ApiModel):
    """One answer a wide IP can give."""

    address: str | None = Field(default=None, description="An IPv4 or IPv6 address.")
    virtual_service: str | None = Field(
        default=None, description="Instead of `address`: a virtual service; its VIP is followed."
    )
    site: str | None = Field(
        default=None, description="A label for the site (a lab host, a data centre)."
    )
    weight: int = Field(default=1, ge=0, le=1000, description="`weighted` only.")
    priority: int = Field(default=1, ge=1, le=10, description="`failover` only: 1 is tried first.")
    subnets: list[str] = Field(
        default_factory=list,
        description="`subnet` only: clients in these CIDR blocks get this member.",
    )


class WideIpIn(ApiModel):
    """A name whose answer follows the health of its members."""

    name: str = Field(description="`@` (the zone apex) or a name inside the zone, like `shop`.")
    algorithm: Literal["failover", "weighted", "all", "subnet", "geo", "latency"] = "failover"
    ttl: int = Field(default=30, ge=5, le=3600)
    health: WideIpHealth = Field(default_factory=WideIpHealth)
    members: list[WideIpMember]
    enabled: bool = True


class WideIpPatch(ApiModel):
    """Fields of a wide IP that can change; omitted fields stay as they are."""

    algorithm: Literal["failover", "weighted", "all", "subnet", "geo", "latency"] | None = None
    ttl: int | None = Field(default=None, ge=5, le=3600)
    health: WideIpHealth | None = None
    members: list[WideIpMember] | None = None
    enabled: bool | None = None


class WideIpMemberRead(WideIpMember):
    """A member as read back, with what the latency probe measured."""

    latency_ms: float | None = Field(
        default=None,
        description="`latency` only: smoothed connect time to the member in milliseconds, or "
        "null when it was never measured or did not answer.",
    )


class WideIpRead(ApiModel):
    """A wide IP."""

    id: UUID
    name: str
    zone: str
    fqdn: str
    algorithm: str
    ttl: int
    health: WideIpHealth
    members: list[WideIpMemberRead]
    latency_order: list[int] = Field(
        default_factory=list,
        description="`latency` only: member indexes from fastest to slowest, as last answered.",
    )
    enabled: bool
    version: int
    applied_at: datetime | None
    last_error: str | None
    created_by: str
    created_at: datetime


class WideIpState(ApiModel):
    """A wide IP against what PowerDNS holds."""

    fqdn: str
    state: Literal["in_sync", "drift", "pending", "error", "off", "unknown"]
    last_error: str | None
    applied_at: datetime | None


class GslbSyncResult(ApiModel):
    """What a sync did."""

    written: int
    failed: int
