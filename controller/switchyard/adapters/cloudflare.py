# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Cloudflare adapter: DNS-01 TXT records, Origin CA certificates and the published IP ranges.

Tokens are scoped per zone (DNS:Edit, plus SSL and Certificates:Edit for Origin CA) and come from
the secrets backend. :class:`HttpCloudflare` is the real client; :class:`FakeCloudflare` records
what was asked for, for tests and for the ACME tests that need a DNS provider.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from switchyard.adapters.dns.base import DnsError
from switchyard.adapters.dns.zones import zone_candidates

API = "https://api.cloudflare.com/client/v4"


class CloudflareError(DnsError):
    """A Cloudflare call failed."""


@dataclass(frozen=True)
class IpRanges:
    """Cloudflare's published source networks."""

    ipv4: tuple[str, ...]
    ipv6: tuple[str, ...]

    @property
    def all(self) -> tuple[str, ...]:
        """IPv4 then IPv6 networks."""
        return self.ipv4 + self.ipv6


class Cloudflare(Protocol):
    """What the controller needs from Cloudflare."""

    async def create_txt(self, name: str, value: str, *, ttl: int = 60) -> str:
        """Create a TXT record in the zone that contains ``name``; return its record id."""
        ...

    async def delete_txt(self, name: str, record_id: str) -> None:
        """Delete a TXT record (idempotent)."""
        ...

    async def issue_origin_ca(self, hostnames: tuple[str, ...], csr_pem: str) -> str:
        """Issue an Origin CA certificate for the CSR and return the certificate PEM."""
        ...

    async def ip_ranges(self) -> IpRanges:
        """Return the published Cloudflare source networks."""
        ...

    async def close(self) -> None:
        """Release connections."""
        ...


class HttpCloudflare:
    """Cloudflare REST client with a scoped API token."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = API,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create a client authenticated with ``token``."""
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=30.0,
        )
        self._zones: dict[str, str] = {}

    async def _call(
        self, method: str, path: str, *, params: dict[str, str] | None = None, body: Any = None
    ) -> Any:
        try:
            response = await self._http.request(method, path, params=params, json=body)
        except httpx.HTTPError as exc:
            raise CloudflareError(f"{method} {path}: {exc}") from exc
        data = response.json() if response.content else {}
        if response.status_code >= 400 or not data.get("success", True):
            errors = "; ".join(str(e.get("message")) for e in data.get("errors", [])) or "failed"
            raise CloudflareError(f"{method} {path}: HTTP {response.status_code}: {errors}")
        return data.get("result")

    async def _zone_id(self, name: str) -> str:
        for candidate in zone_candidates(name):
            if candidate in self._zones:
                return self._zones[candidate]
            found = await self._call("GET", "/zones", params={"name": candidate})
            if found:
                self._zones[candidate] = str(found[0]["id"])
                return self._zones[candidate]
        raise CloudflareError(f"no Cloudflare zone found for {name}")

    async def create_txt(self, name: str, value: str, *, ttl: int = 60) -> str:
        """Create a TXT record (TTL is at least 60 seconds)."""
        zone = await self._zone_id(name)
        record = await self._call(
            "POST",
            f"/zones/{zone}/dns_records",
            body={"type": "TXT", "name": name, "content": value, "ttl": max(ttl, 60)},
        )
        return f"{zone}/{record['id']}"

    async def delete_txt(self, name: str, record_id: str) -> None:
        """Delete a record created by :meth:`create_txt`."""
        zone, _, rid = record_id.partition("/")
        try:
            await self._call("DELETE", f"/zones/{zone}/dns_records/{rid}")
        except CloudflareError as exc:
            if "404" not in str(exc):
                raise

    async def upsert_address(
        self, name: str, address: str, *, ttl: int, proxied: bool, handle: str | None
    ) -> str:
        """Create or update an ``A``/``AAAA`` record (ADR 0193).

        A record found by name that this controller did not create is adopted only when its
        content already equals ``address``; otherwise the call fails and nothing is changed.
        """
        zone = await self._zone_id(name)
        rtype = "AAAA" if ":" in address else "A"
        body = {
            "type": rtype,
            "name": name,
            "content": address,
            "ttl": 1 if proxied else max(ttl, 60),
            "proxied": proxied,
        }
        if handle is not None:
            zone_id, _, rid = handle.partition("/")
            try:
                record = await self._call("PUT", f"/zones/{zone_id}/dns_records/{rid}", body=body)
            except CloudflareError as exc:
                if "404" not in str(exc):
                    raise
                handle = None  # deleted by hand; fall through and look again
            else:
                return f"{zone_id}/{record['id']}"
        found = await self._call(
            "GET", f"/zones/{zone}/dns_records", params={"type": rtype, "name": name}
        )
        if found:
            existing = found[0]
            if existing.get("content") != address:
                raise CloudflareError(
                    f"{name} already has a {rtype} record with another value "
                    f"({existing.get('content')}) that this controller did not create"
                )
            record = await self._call(
                "PUT", f"/zones/{zone}/dns_records/{existing['id']}", body=body
            )
            return f"{zone}/{record['id']}"
        record = await self._call("POST", f"/zones/{zone}/dns_records", body=body)
        return f"{zone}/{record['id']}"

    async def delete_address(self, name: str, handle: str) -> None:
        """Delete a record created by :meth:`upsert_address`."""
        await self.delete_txt(name, handle)

    async def issue_origin_ca(self, hostnames: tuple[str, ...], csr_pem: str) -> str:
        """Request an ECDSA Origin CA certificate valid for 15 years."""
        result = await self._call(
            "POST",
            "/certificates",
            body={
                "hostnames": list(hostnames),
                "requested_validity": 5475,
                "request_type": "origin-ecc",
                "csr": csr_pem,
            },
        )
        return str(result["certificate"])

    async def ip_ranges(self) -> IpRanges:
        """Return the published ranges (an unauthenticated endpoint)."""
        result = await self._call("GET", "/ips")
        return IpRanges(tuple(result["ipv4_cidrs"]), tuple(result["ipv6_cidrs"]))

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()


@dataclass
class FakeCloudflare:
    """In-memory Cloudflare.

    Attributes:
        txt: Live TXT records as ``record id -> (name, value)``.
        history: Every TXT value ever created, in order (the records are deleted after use).
        origin_ca: Callable that turns ``(hostnames, csr_pem)`` into a certificate PEM; tests
            plug in a signer.
        ranges: What :meth:`ip_ranges` returns.
        fail_on: Operation names that raise once.
        addresses: Address records as ``record id -> (name, type, content, ttl, proxied)``; a test
            can add one by hand to stand for a record the controller did not create.
        owned: The ids in ``addresses`` that :meth:`upsert_address` created.
    """

    txt: dict[str, tuple[str, str]] = field(default_factory=dict)
    history: list[tuple[str, str]] = field(default_factory=list)
    origin_ca: Any = None
    ranges: IpRanges = IpRanges(("192.0.2.0/24",), ("2001:db8::/32",))
    fail_on: set[str] = field(default_factory=set)
    addresses: dict[str, tuple[str, str, str, int, bool]] = field(default_factory=dict)
    owned: set[str] = field(default_factory=set)
    _n: int = 0

    def _check(self, op: str) -> None:
        if op in self.fail_on:
            self.fail_on.discard(op)
            raise CloudflareError(f"injected failure in {op}")

    async def create_txt(self, name: str, value: str, *, ttl: int = 60) -> str:
        """Record the TXT value."""
        self._check("create_txt")
        self._n += 1
        rid = f"rec{self._n}"
        self.txt[rid] = (name, value)
        self.history.append((name, value))
        return rid

    async def delete_txt(self, name: str, record_id: str) -> None:
        """Forget the record."""
        self._check("delete_txt")
        self.txt.pop(record_id, None)

    async def upsert_address(
        self, name: str, address: str, *, ttl: int, proxied: bool, handle: str | None
    ) -> str:
        """Create or update an address record; refuse a foreign record with another value."""
        self._check("upsert_address")
        rtype = "AAAA" if ":" in address else "A"
        if handle is not None and handle in self.addresses:
            self.addresses[handle] = (name, rtype, address, ttl, proxied)
            return handle
        for rid, (n, t, content, _, _) in self.addresses.items():
            if (n, t) == (name, rtype) and rid not in self.owned:
                if content != address:
                    raise CloudflareError(f"{name} already has a {rtype} record with another value")
                self.addresses[rid] = (name, rtype, address, ttl, proxied)
                return rid
        self._n += 1
        rid = f"addr{self._n}"
        self.addresses[rid] = (name, rtype, address, ttl, proxied)
        self.owned.add(rid)
        return rid

    async def delete_address(self, name: str, handle: str) -> None:
        """Forget the record."""
        self._check("delete_address")
        self.addresses.pop(handle, None)
        self.owned.discard(handle)

    async def issue_origin_ca(self, hostnames: tuple[str, ...], csr_pem: str) -> str:
        """Sign through the configured callable."""
        self._check("issue_origin_ca")
        if self.origin_ca is None:
            raise CloudflareError("no Origin CA signer configured in the fake")
        return str(self.origin_ca(hostnames, csr_pem))

    async def ip_ranges(self) -> IpRanges:
        """Return the configured ranges."""
        self._check("ip_ranges")
        return self.ranges

    async def close(self) -> None:
        """Nothing to release."""
