"""Build a DNS provider from a stored credential (its settings and its secrets)."""

from collections.abc import Mapping
from typing import Any

import httpx

from switchyard.adapters.cloudflare import HttpCloudflare
from switchyard.adapters.dns.azure import AzureDns
from switchyard.adapters.dns.base import (
    CATALOGUE,
    DnsError,
    DnsProvider,
    check_endpoint,
    check_settings,
)
from switchyard.adapters.dns.google import GoogleDns
from switchyard.adapters.dns.powerdns import PowerDns
from switchyard.adapters.dns.rfc2136 import Rfc2136
from switchyard.adapters.dns.route53 import AwsCredentials, Route53


def build_provider(
    type_: str,
    config: Mapping[str, Any],
    credentials: Mapping[str, Any],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DnsProvider:
    """Create the client for a provider type.

    Servers named by the credential (PowerDNS, RFC 2136) are checked against the endpoint policy
    each time, so a name that later points at an internal service is refused.

    Raises:
        DnsError: If the settings are incomplete or name an address the controller may not use.

    Example:
        >>> build_provider("cloudflare", {}, {"api_token": "t"}).__class__.__name__
        'HttpCloudflare'
    """
    problems = check_settings(type_, config, credentials)
    if problems:
        raise DnsError("; ".join(f"{path}: {text}" for path, text in sorted(problems.items())))
    defaults = {f.key: f.default for f in CATALOGUE[type_].fields if f.default}
    cfg: dict[str, Any] = {**defaults, **{k: v for k, v in config.items() if v not in (None, "")}}
    if type_ == "cloudflare":
        return HttpCloudflare(credentials["api_token"], transport=transport)
    if type_ == "route53":
        return Route53(
            AwsCredentials(
                credentials["access_key_id"],
                credentials["secret_access_key"],
                credentials.get("session_token") or None,
            ),
            hosted_zone_id=cfg.get("hosted_zone_id"),
            role_arn=cfg.get("role_arn"),
            transport=transport,
        )
    if type_ == "azure":
        return AzureDns(
            azure_tenant_id=cfg["azure_tenant_id"],
            client_id=cfg["client_id"],
            client_secret=credentials["client_secret"],
            subscription_id=cfg["subscription_id"],
            resource_group=cfg["resource_group"],
            zone=cfg["zone"],
            cloud=cfg.get("cloud", "public"),
            transport=transport,
        )
    if type_ == "google":
        return GoogleDns(
            credentials["service_account_json"],
            cfg["project_id"],
            cfg["managed_zone"],
            cfg["zone"],
            transport=transport,
        )
    if type_ == "powerdns":
        host = httpx.URL(cfg["api_url"]).host
        try:
            check_endpoint(host, "api_url")
        except ValueError as exc:
            raise DnsError(str(exc)) from exc
        return PowerDns(
            cfg["api_url"],
            credentials["api_key"],
            cfg["zone"],
            server_id=cfg.get("server_id", "localhost"),
            transport=transport,
        )
    if type_ == "rfc2136":
        try:
            check_endpoint(cfg["server"], "server")
            port = int(cfg.get("port", 53))
        except ValueError as exc:
            raise DnsError(str(exc)) from exc
        return Rfc2136(
            cfg["server"],
            cfg["zone"],
            cfg["key_name"],
            credentials["tsig_secret"],
            port=port,
            algorithm=cfg.get("algorithm", "hmac-sha256"),
        )
    raise DnsError(f"unknown provider type {type_!r}")  # pragma: no cover
