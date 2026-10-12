"""PowerDNS Authoritative Server HTTP API: TXT record sets.

A record set holds every value of a name, so adding or removing one value reads the set and
replaces it.
"""

from typing import Any

import httpx

from switchyard.adapters.dns.base import DnsError
from switchyard.adapters.dns.zones import in_zone


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class PowerDns:
    """PowerDNS TXT records for ACME DNS-01."""

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
            headers={"X-API-Key": api_key}, transport=transport, timeout=30.0
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

    async def _values(self, name: str) -> tuple[int, list[str]]:
        response = await self._request(
            "GET", params={"rrset_name": f"{name}.", "rrset_type": "TXT"}
        )
        for rrset in response.json().get("rrsets", []):
            if rrset.get("type") == "TXT" and rrset.get("name", "").rstrip(".") == name.rstrip("."):
                records = [r["content"] for r in rrset.get("records", []) if not r.get("disabled")]
                return int(rrset.get("ttl", 60)), records
        return 60, []

    async def _replace(self, name: str, ttl: int, values: list[str]) -> None:
        rrset: dict[str, Any] = {"name": f"{name}.", "type": "TXT", "ttl": ttl}
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
        current_ttl, values = await self._values(name)
        quoted = _quote(value)
        if quoted not in values:
            await self._replace(name, current_ttl if values else ttl, [*values, quoted])
        return value

    async def delete_txt(self, name: str, handle: str) -> None:
        """Remove ``handle`` (the value) from the TXT set of ``name``."""
        ttl, values = await self._values(name)
        quoted = _quote(handle)
        if quoted in values:
            await self._replace(name, ttl, [v for v in values if v != quoted])

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()


def _error(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", response.text))[:300]
    except ValueError:
        return response.text[:200]
