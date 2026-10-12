"""Azure DNS: TXT record sets through Azure Resource Manager, with a service principal.

The token comes from the client-credentials flow. A record set holds every value of a name, so the
set is read, changed and written back for each value that is added or removed.
"""

import time
from typing import Any

import httpx

from switchyard.adapters.dns.base import DnsError
from switchyard.adapters.dns.zones import in_zone, relative_name

#: ``cloud -> (login host, management host)``
CLOUDS = {
    "public": ("login.microsoftonline.com", "management.azure.com"),
    "usgov": ("login.microsoftonline.us", "management.usgovcloudapi.net"),
    "china": ("login.chinacloudapi.cn", "management.chinacloudapi.cn"),
}
API_VERSION = "2018-05-01"


class AzureDns:
    """Azure DNS TXT records for ACME DNS-01."""

    def __init__(
        self,
        *,
        azure_tenant_id: str,
        client_id: str,
        client_secret: str,
        subscription_id: str,
        resource_group: str,
        zone: str,
        cloud: str = "public",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create the client for one DNS zone."""
        if cloud not in CLOUDS:
            raise DnsError(f"unknown Azure cloud {cloud!r}")
        self._login, self._mgmt = CLOUDS[cloud]
        self._tenant = azure_tenant_id
        self._client = client_id
        self._secret = client_secret
        self._zone = zone.strip(".")
        self._base = (
            f"https://{self._mgmt}/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
            f"/providers/Microsoft.Network/dnsZones/{self._zone}"
        )
        self._http = httpx.AsyncClient(transport=transport, timeout=30.0)
        self._token: str | None = None
        self._expires = 0.0

    async def _bearer(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        try:
            response = await self._http.post(
                f"https://{self._login}/{self._tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._client,
                    "client_secret": self._secret,
                    "scope": f"https://{self._mgmt}/.default",
                },
            )
        except httpx.HTTPError as exc:
            raise DnsError(f"Azure sign-in: {exc}") from exc
        if response.status_code != 200:
            detail = _message(response)
            raise DnsError(f"Azure sign-in: HTTP {response.status_code}: {detail}")
        body = response.json()
        self._token = str(body["access_token"])
        self._expires = time.time() + float(body.get("expires_in", 3000))
        return self._token

    async def _call(self, method: str, name: str, body: Any = None) -> httpx.Response:
        if not in_zone(name, self._zone):
            raise DnsError(f"{name} is not inside the Azure zone {self._zone}")
        url = f"{self._base}/TXT/{relative_name(name, self._zone)}"
        try:
            response = await self._http.request(
                method,
                url,
                params={"api-version": API_VERSION},
                json=body,
                headers={"Authorization": f"Bearer {await self._bearer()}"},
            )
        except httpx.HTTPError as exc:
            raise DnsError(f"Azure DNS {method} {name}: {exc}") from exc
        if response.status_code >= 400 and response.status_code != 404:
            raise DnsError(
                f"Azure DNS {method} {name}: HTTP {response.status_code}: {_message(response)}"
            )
        return response

    async def _values(self, name: str) -> tuple[int, list[str]]:
        response = await self._call("GET", name)
        if response.status_code == 404:
            return 60, []
        props = response.json().get("properties", {})
        values = [v for record in props.get("TXTRecords", []) for v in record.get("value", [])]
        return int(props.get("TTL", 60)), values

    async def _put(self, name: str, ttl: int, values: list[str]) -> None:
        body = {"properties": {"TTL": ttl, "TXTRecords": [{"value": [v]} for v in values]}}
        await self._call("PUT", name, body)

    async def create_txt(self, name: str, value: str, *, ttl: int = 60) -> str:
        """Add ``value`` to the TXT set of ``name``."""
        current_ttl, values = await self._values(name)
        if value not in values:
            await self._put(name, current_ttl if values else max(ttl, 60), [*values, value])
        return value

    async def delete_txt(self, name: str, handle: str) -> None:
        """Remove ``handle`` (the value) from the TXT set of ``name``."""
        ttl, values = await self._values(name)
        if handle not in values:
            return
        remaining = [v for v in values if v != handle]
        if remaining:
            await self._put(name, ttl, remaining)
        else:
            await self._call("DELETE", name)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or body)[:300]
    return str(body.get("error_description") or error or body)[:300]
