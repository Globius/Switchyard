# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""DNS providers for ACME DNS-01: the interface, the catalogue of provider types and their forms.

Every provider publishes and removes TXT records. A name can carry several TXT values at once
(``example.test`` and ``*.example.test`` both validate at ``_acme-challenge.example.test``), so
providers that replace whole record sets must add and remove single values. The handle that
``create_txt`` returns is opaque to the caller and is passed back to ``delete_txt``.

The catalogue describes each provider type's fields (name, label, kind, required, secret), so the
API and the web UI can render one generic form. Secret fields are write-only: they go to the secrets
backend and are never returned.
"""

import ipaddress
import os
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class DnsError(Exception):
    """A DNS provider call failed."""


class DnsProvider(Protocol):
    """What ACME needs from a DNS provider."""

    async def create_txt(self, name: str, value: str, *, ttl: int = 60) -> str:
        """Add a TXT value at ``name`` and return a handle for :meth:`delete_txt`."""
        ...

    async def delete_txt(self, name: str, handle: str) -> None:
        """Remove what :meth:`create_txt` added (idempotent)."""
        ...

    async def close(self) -> None:
        """Release connections."""
        ...


class AddressRecords(Protocol):
    """Providers that can also publish ``A``/``AAAA`` records for a virtual service (ADR 0193).

    Only Cloudflare implements it so far. :func:`supports_addresses` tells them apart.
    """

    async def upsert_address(
        self, name: str, address: str, *, ttl: int, proxied: bool, handle: str | None
    ) -> str:
        """Make ``name`` resolve to ``address`` and return the record's handle.

        With a ``handle`` the record the controller created earlier is updated. Without one an
        existing record with the same content is adopted and one with different content is
        refused (:class:`DnsError`): the controller never overwrites a record it did not create.
        """
        ...

    async def delete_address(self, name: str, handle: str) -> None:
        """Remove a record created by :meth:`upsert_address` (idempotent)."""
        ...


def supports_addresses(provider: object) -> bool:
    """True if the provider can publish address records.

    Example:
        >>> supports_addresses(object())
        False
    """
    return callable(getattr(provider, "upsert_address", None))


@dataclass(frozen=True)
class Field:
    """One field of a provider's form.

    Attributes:
        key: Name in ``config`` (or in ``credentials`` when ``secret``).
        label: Text for the form.
        kind: ``text``, ``select`` or ``textarea``.
        required: The provider cannot work without it.
        secret: Write-only credential: stored in the secrets backend, never returned.
        help: One sentence of guidance.
        options: Allowed values of a ``select``.
        default: Value used when the field is left empty.
    """

    key: str
    label: str
    kind: str = "text"
    required: bool = True
    secret: bool = False
    help: str = ""
    options: tuple[str, ...] = ()
    default: str | None = None


@dataclass(frozen=True)
class ProviderType:
    """A kind of DNS provider.

    Attributes:
        type: Identifier (``cloudflare``, ``route53`` ...).
        label: Name for people.
        fields: The form.
        supports_zone_discovery: The provider finds the zone of a name itself, so ``zones`` on the
            credential is only used to choose between credentials.
    """

    type: str
    label: str
    fields: tuple[Field, ...]
    supports_zone_discovery: bool


_ALGORITHMS = ("hmac-sha256", "hmac-sha384", "hmac-sha512")
_CLOUDS = ("public", "usgov", "china")

CATALOGUE: dict[str, ProviderType] = {
    t.type: t
    for t in (
        ProviderType(
            "cloudflare",
            "Cloudflare",
            (
                Field(
                    "api_token",
                    "API token",
                    secret=True,
                    help="A token with Zone:Read and DNS:Edit on the zones you validate.",
                ),
            ),
            True,
        ),
        ProviderType(
            "route53",
            "Amazon Route 53",
            (
                Field("access_key_id", "Access key ID", secret=True),
                Field("secret_access_key", "Secret access key", secret=True),
                Field(
                    "session_token",
                    "Session token",
                    required=False,
                    secret=True,
                    help="Only for temporary credentials.",
                ),
                Field(
                    "hosted_zone_id",
                    "Hosted zone ID",
                    required=False,
                    help="Leave empty to find the public zone of each name.",
                ),
                Field(
                    "role_arn",
                    "Role to assume",
                    required=False,
                    help="An IAM role ARN to assume with these credentials.",
                ),
            ),
            True,
        ),
        ProviderType(
            "azure",
            "Azure DNS",
            (
                Field("azure_tenant_id", "Directory (tenant) ID"),
                Field("client_id", "Application (client) ID"),
                Field("client_secret", "Client secret", secret=True),
                Field("subscription_id", "Subscription ID"),
                Field("resource_group", "Resource group"),
                Field("zone", "DNS zone", help="The Azure DNS zone that holds the records."),
                Field(
                    "cloud",
                    "Cloud",
                    kind="select",
                    required=False,
                    options=_CLOUDS,
                    default="public",
                ),
            ),
            False,
        ),
        ProviderType(
            "google",
            "Google Cloud DNS",
            (
                Field(
                    "service_account_json",
                    "Service account key",
                    kind="textarea",
                    secret=True,
                    help="The JSON key file of a service account that may edit the zone.",
                ),
                Field("project_id", "Project ID"),
                Field("managed_zone", "Managed zone", help="The zone's name in Cloud DNS."),
                Field("zone", "DNS name", help="The zone's DNS name, for example example.test."),
            ),
            False,
        ),
        ProviderType(
            "rfc2136",
            "RFC 2136 (BIND, Knot, PowerDNS ...)",
            (
                Field("server", "Server", help="Address of the name server that takes updates."),
                Field("port", "Port", required=False, default="53"),
                Field("zone", "Zone", help="The zone the server is authoritative for."),
                Field("key_name", "TSIG key name"),
                Field(
                    "algorithm",
                    "TSIG algorithm",
                    kind="select",
                    required=False,
                    options=_ALGORITHMS,
                    default="hmac-sha256",
                ),
                Field(
                    "tsig_secret", "TSIG secret", secret=True, help="Base64, as in the key file."
                ),
            ),
            False,
        ),
        ProviderType(
            "powerdns",
            "PowerDNS API",
            (
                Field("api_url", "API URL", help="For example https://pdns.example.test:8081."),
                Field("server_id", "Server ID", required=False, default="localhost"),
                Field("zone", "Zone"),
                Field("api_key", "API key", secret=True),
            ),
            False,
        ),
    )
}


def types() -> list[ProviderType]:
    """The provider types in a stable order."""
    return list(CATALOGUE.values())


def check_settings(
    type_: str,
    config: Mapping[str, Any],
    credentials: Mapping[str, Any],
    *,
    secrets_required: bool = True,
) -> dict[str, str]:
    """Check a provider's fields; return ``field path -> message`` (empty when fine).

    Unknown fields are rejected so a typo does not silently drop a setting.

    Args:
        type_: Provider type.
        config: The non-secret fields.
        credentials: The secret fields.
        secrets_required: False when only changing config (the stored credentials stay).

    Example:
        >>> check_settings("cloudflare", {}, {"api_token": "t"})
        {}
        >>> sorted(check_settings("cloudflare", {"x": "1"}, {}))
        ['config.x', 'credentials.api_token']
    """
    spec = CATALOGUE.get(type_)
    if spec is None:
        return {"type": f"unknown provider type {type_!r}"}
    problems: dict[str, str] = {}
    plain = {f.key: f for f in spec.fields if not f.secret}
    secret = {f.key: f for f in spec.fields if f.secret}
    for key in config:
        if key not in plain:
            problems[f"config.{key}"] = "not a setting of this provider"
    for key in credentials:
        if key not in secret:
            problems[f"credentials.{key}"] = "not a credential of this provider"
    for key, field in plain.items():
        value = config.get(key)
        if field.required and not str(value or "").strip():
            problems[f"config.{key}"] = "required"
        elif field.options and value and value not in field.options:
            problems[f"config.{key}"] = f"must be one of {', '.join(field.options)}"
    if secrets_required:
        for key, field in secret.items():
            if field.required and not str(credentials.get(key) or "").strip():
                problems[f"credentials.{key}"] = "required"
    for group, values in (("config", config), ("credentials", credentials)):
        for key, value in values.items():
            if not isinstance(value, str) or len(value) > 4096:
                problems.setdefault(f"{group}.{key}", "must be text of at most 4096 characters")
    return problems


def policy_open() -> bool:
    """True when ``SWITCHYARD_ENGINE_POLICY=open`` allows private addresses (labs)."""
    return os.environ.get("SWITCHYARD_ENGINE_POLICY", "strict") == "open"


def allowed_dns_networks(
    env: str = "SWITCHYARD_DNS_CIDRS",
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Private networks a named server may live in: the variable ``env``, comma separated."""
    raw = os.environ.get(env, "")
    return [ipaddress.ip_network(p.strip()) for p in raw.split(",") if p.strip()]


def check_endpoint(host: str, what: str, *, env: str = "SWITCHYARD_DNS_CIDRS") -> None:
    """Raise ``ValueError`` unless the controller may connect to ``host`` for DNS updates.

    A tenant administrator names the server, so the address must not be loopback, link-local
    (cloud metadata), multicast or unspecified, and private addresses need an operator allow list
    (the variable ``env``, ``SWITCHYARD_DNS_CIDRS`` by default). Every address the name resolves
    to is checked.

    Example:
        >>> check_endpoint("169.254.169.254", "server")
        Traceback (most recent call last):
            ...
        ValueError: server: 169.254.169.254 is not an address the controller may contact
    """
    if not host:
        raise ValueError(f"{what}: no host")
    try:
        addresses = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        try:
            found = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError(f"{what}: cannot resolve {host}") from exc
        addresses = [ipaddress.ip_address(item[4][0]) for item in found]
    allow = allowed_dns_networks(env)
    for address in addresses:
        if policy_open():
            continue
        blocked = (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        )
        # `not is_global` also covers shared address space (100.64.0.0/10, which holds some
        # clouds' metadata address), not only the RFC 1918 ranges that `is_private` names.
        if blocked or (not address.is_global and not any(address in net for net in allow)):
            raise ValueError(f"{what}: {address} is not an address the controller may contact")
