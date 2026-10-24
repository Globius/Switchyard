# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""PowerDNS Authoritative Server HTTP API: TXT and address record sets.

A record set holds every value of a name, so adding or removing one value reads the set and
replaces it. Address records (``A``/``AAAA``) are the same shape: the record the controller
created is identified by its value, so a value someone else put in the set is never removed.
"""

from functools import partial
from typing import Any

import httpx

from switchyard.adapters.dns.base import DnsError, address_target
from switchyard.adapters.dns.zones import in_zone
from switchyard.adapters.endpoint import pin_request

_NO_PROXY = "PowerDNS has no proxy: set proxied to false for records on this credential"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class PowerDns:
    """PowerDNS records for ACME DNS-01 and for a virtual service (ADR 0193)."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        zone: str,
        *,
        server_id: str = "localhost",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create the client for one zone of one server."""
        self._zone = zone.strip(".")
        self._url = f"{api_url.rstrip('/')}/api/v1/servers/{server_id}/zones/{self._zone}."
        self._http = httpx.AsyncClient(
            headers={"X-API-Key": api_key},
            transport=transport,
            timeout=30.0,
            event_hooks={
                "request": [partial(pin_request, what="api_url", env="SWITCHYARD_DNS_CIDRS")]
                if transport is None
                else []
            },
        )

    async def _request(
        self, method: str, params: dict[str, str] | None = None, body: Any = None
    ) -> httpx.Response:
        try:
            response = await self._http.request(method, self._url, params=params, json=body)
        except httpx.HTTPError as exc:
            raise DnsError(f"PowerDNS {method}: {exc}") from exc
        if response.status_code >= 400:
            raise DnsError(f"PowerDNS {method}: HTTP {response.status_code}: {_error(response)}")
        return response

    async def _rrset(self, name: str, rtype: str) -> tuple[int, list[str]]:
        """The live values of one record set of ``name`` and its TTL (empty when there is none)."""
        response = await self._request(
            "GET", params={"rrset_name": f"{name}.", "rrset_type": rtype}
        )
        for rrset in response.json().get("rrsets", []):
            if rrset.get("type") == rtype and rrset.get("name", "").rstrip(".") == name.rstrip("."):
                records = [r["content"] for r in rrset.get("records", []) if not r.get("disabled")]
                return int(rrset.get("ttl", 60)), records
        return 60, []

    async def _put(self, name: str, rtype: str, ttl: int, values: list[str]) -> None:
        """Replace one record set (delete it when ``values`` is empty)."""
        rrset: dict[str, Any] = {"name": f"{name}.", "type": rtype, "ttl": ttl}
        if values:
            rrset |= {
                "changetype": "REPLACE",
                "records": [{"content": v, "disabled": False} for v in values],
            }
        else:
            rrset["changetype"] = "DELETE"
        await self._request("PATCH", body={"rrsets": [rrset]})

    async def create_txt(self, name: str, value: str, *, ttl: int = 60) -> str:
        """Add ``value`` to the TXT set of ``name``."""
        if not in_zone(name, self._zone):
            raise DnsError(f"{name} is not inside the PowerDNS zone {self._zone}")
        current_ttl, values = await self._rrset(name, "TXT")
        quoted = _quote(value)
        if quoted not in values:
            await self._put(name, "TXT", current_ttl if values else ttl, [*values, quoted])
        return value

    async def delete_txt(self, name: str, handle: str) -> None:
        """Remove ``handle`` (the value) from the TXT set of ``name``."""
        ttl, values = await self._rrset(name, "TXT")
        quoted = _quote(handle)
        if quoted in values:
            await self._put(name, "TXT", ttl, [v for v in values if v != quoted])

    async def upsert_address(
        self, name: str, address: str, *, ttl: int, proxied: bool, handle: str | None
    ) -> str:
        """Create or update an ``A``/``AAAA`` record (ADR 0193); the handle is its value.

        A record found by name that this controller did not create is adopted only when its value
        already equals ``address``; otherwise the call fails and nothing is changed.
        """
        clean, rtype, value = address_target(name, address, self._zone)
        if proxied:
            raise DnsError(_NO_PROXY)
        current_ttl, values = await self._rrset(clean, rtype)
        known = handle.strip() if handle is not None else None
        if known is not None and known in values:
            rest = [v for v in values if v != known]
            # any other value in the set was not ours: keep it, replace only our own
            await self._put(clean, rtype, ttl, rest if value in rest else [*rest, value])
            return value
        foreign = [v for v in values if v != value]
        if foreign:
            raise DnsError(
                f"{clean} already has an {rtype} record ({', '.join(foreign)}) that this "
                "controller did not create"
            )
        if value in values and current_ttl == ttl:
            return value
        await self._put(clean, rtype, ttl, [value])
        return value

    async def delete_address(self, name: str, handle: str) -> None:
        """Remove the record ``handle`` (its value) from ``name`` (idempotent)."""
        clean, rtype, value = address_target(name, handle, self._zone)
        ttl, values = await self._rrset(clean, rtype)
        if value in values:
            await self._put(clean, rtype, ttl, [v for v in values if v != value])

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()


def _error(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", response.text))[:300]
    except ValueError:
        return response.text[:200]
