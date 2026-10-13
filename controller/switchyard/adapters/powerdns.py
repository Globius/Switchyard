# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""PowerDNS Authoritative Server HTTP API, for GSLB zones and wide IP records (ADR 0160).

:class:`HttpPowerDns` is the real client; :class:`FakePowerDns` keeps zones in memory for tests. The
server must run with ``enable-lua-records=yes`` (the controller cannot change that), and the API key
is the controller's only credential.
"""

import contextlib
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from switchyard.domain.gslb import Rrset


class PowerDnsError(Exception):
    """PowerDNS failed or answered something unexpected; the message is safe to show."""


def fqdn(name: str) -> str:
    """The name with a trailing dot, as PowerDNS wants it.

    Example:
        >>> fqdn("www.example.test")
        'www.example.test.'
    """
    return name if name.endswith(".") else name + "."


class PowerDns(Protocol):
    """What GSLB needs from the authoritative server."""

    async def ensure_zone(self, zone: str, nameservers: list[str]) -> None:
        """Create the zone (native, with an SOA and these NS records) if it does not exist."""
        ...

    async def delete_zone(self, zone: str) -> None:
        """Remove the zone and everything in it (no error if it is gone)."""
        ...

    async def put_rrsets(self, zone: str, rrsets: list[Rrset]) -> None:
        """Replace these record sets."""
        ...

    async def delete_rrsets(self, zone: str, names: list[tuple[str, str]]) -> None:
        """Remove record sets by ``(name, type)``."""
        ...

    async def get_rrsets(self, zone: str) -> list[Rrset]:
        """The ``LUA`` record sets currently in the zone."""
        ...

    async def enable_dnssec(self, zone: str) -> list[str]:
        """Turn DNSSEC on for the zone and return its DS records (to publish at the parent)."""
        ...

    async def ds_records(self, zone: str) -> list[str]:
        """The zone's DS records, or none when DNSSEC is off."""
        ...


class HttpPowerDns:
    """The real client."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        server_id: str = "localhost",
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        """Create the client.

        Args:
            base_url: The webserver address (``http://pdns:8081``).
            api_key: The API key (``api-key`` in pdns.conf).
            server_id: PowerDNS server id (``localhost``).
            client: Injected HTTP client (tests).
            timeout: Seconds per request.
        """
        self._base = f"{base_url.rstrip('/')}/api/v1/servers/{quote(server_id, safe='')}"
        self._headers = {"X-API-Key": api_key}
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def _call(
        self, method: str, path: str, *, json: Any = None, ok: tuple[int, ...] = ()
    ) -> httpx.Response:
        try:
            response = await self._client.request(
                method, self._base + path, json=json, headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise PowerDnsError(f"PowerDNS is not reachable: {type(exc).__name__}") from exc
        if response.status_code >= 400 and response.status_code not in ok:
            detail = ""
            with contextlib.suppress(ValueError):
                detail = str(response.json().get("error", ""))[:200]
            raise PowerDnsError(f"PowerDNS answered HTTP {response.status_code} {detail}".strip())
        return response

    @staticmethod
    def _zone(zone: str) -> str:
        return quote(fqdn(zone), safe="")

    async def ensure_zone(self, zone: str, nameservers: list[str]) -> None:
        """Create the zone unless it exists (409 is fine: another node was first)."""
        found = await self._call("GET", f"/zones/{self._zone(zone)}", ok=(404,))
        if found.status_code != 404:
            return
        # Without an SOA of our own PowerDNS fills in its `default-soa-name`, which is the invalid
        # name `a.misconfigured.dns.server.invalid.` unless the operator set it (found in the lab).
        primary = fqdn(nameservers[0]) if nameservers else f"ns1.{fqdn(zone)}"
        soa = f"{primary} hostmaster.{fqdn(zone)} 1 10800 3600 604800 3600"
        await self._call(
            "POST",
            "/zones",
            json={
                "name": fqdn(zone),
                "kind": "Native",
                "nameservers": [fqdn(n) for n in nameservers],
                "rrsets": [
                    {
                        "name": fqdn(zone),
                        "type": "SOA",
                        "ttl": 3600,
                        "records": [{"content": soa, "disabled": False}],
                    }
                ],
            },
            ok=(409,),
        )

    async def delete_zone(self, zone: str) -> None:
        """Delete the zone."""
        await self._call("DELETE", f"/zones/{self._zone(zone)}", ok=(404,))

    async def put_rrsets(self, zone: str, rrsets: list[Rrset]) -> None:
        """REPLACE the record sets in one request, so the change is atomic."""
        if not rrsets:
            return
        body = {
            "rrsets": [
                {
                    "name": r.name,
                    "type": r.type,
                    "ttl": r.ttl,
                    "changetype": "REPLACE",
                    "records": [{"content": c, "disabled": False} for c in r.records],
                }
                for r in rrsets
            ]
        }
        await self._call("PATCH", f"/zones/{self._zone(zone)}", json=body)

    async def delete_rrsets(self, zone: str, names: list[tuple[str, str]]) -> None:
        """DELETE record sets by name and type."""
        if not names:
            return
        body = {"rrsets": [{"name": n, "type": t, "changetype": "DELETE"} for n, t in names]}
        await self._call("PATCH", f"/zones/{self._zone(zone)}", json=body)

    async def get_rrsets(self, zone: str) -> list[Rrset]:
        """The LUA record sets of the zone."""
        response = await self._call("GET", f"/zones/{self._zone(zone)}")
        return [
            Rrset(r["name"], r["type"], int(r["ttl"]), [c["content"] for c in r["records"]])
            for r in response.json().get("rrsets", [])
            if r["type"] == "LUA"
        ]

    async def enable_dnssec(self, zone: str) -> list[str]:
        """Switch DNSSEC on (PowerDNS creates the keys) and return the DS records."""
        await self._call(
            "PUT", f"/zones/{self._zone(zone)}", json={"dnssec": True, "api_rectify": True}
        )
        return await self.ds_records(zone)

    async def ds_records(self, zone: str) -> list[str]:
        """DS records of the active key-signing keys."""
        response = await self._call("GET", f"/zones/{self._zone(zone)}/cryptokeys")
        out: list[str] = []
        for key in response.json():
            if key.get("keytype") in {"ksk", "csk"} and key.get("active"):
                out += [f"{fqdn(zone)} IN DS {d}" for d in key.get("ds", [])]
        return out


@dataclass
class FakePowerDns:
    """In-memory zones; ``fail`` makes every call raise (to test error handling)."""

    zones: dict[str, dict[tuple[str, str], Rrset]] = field(default_factory=dict)
    nameservers: dict[str, list[str]] = field(default_factory=dict)
    dnssec: set[str] = field(default_factory=set)
    fail: bool = False
    calls: int = 0

    def _check(self) -> None:
        self.calls += 1
        if self.fail:
            raise PowerDnsError("PowerDNS is not reachable: ConnectError")

    async def ensure_zone(self, zone: str, nameservers: list[str]) -> None:
        """Create the zone."""
        self._check()
        self.zones.setdefault(fqdn(zone), {})
        self.nameservers[fqdn(zone)] = list(nameservers)

    async def delete_zone(self, zone: str) -> None:
        """Delete the zone."""
        self._check()
        self.zones.pop(fqdn(zone), None)

    async def put_rrsets(self, zone: str, rrsets: list[Rrset]) -> None:
        """Replace record sets."""
        self._check()
        target = self.zones[fqdn(zone)]
        for r in rrsets:
            target[(r.name, r.type)] = r

    async def delete_rrsets(self, zone: str, names: list[tuple[str, str]]) -> None:
        """Delete record sets."""
        self._check()
        for key in names:
            self.zones[fqdn(zone)].pop(key, None)

    async def get_rrsets(self, zone: str) -> list[Rrset]:
        """Record sets of the zone."""
        self._check()
        return sorted(self.zones[fqdn(zone)].values(), key=lambda r: (r.name, r.type))

    async def enable_dnssec(self, zone: str) -> list[str]:
        """Pretend to sign the zone."""
        self._check()
        self.dnssec.add(fqdn(zone))
        return await self.ds_records(zone)

    async def ds_records(self, zone: str) -> list[str]:
        """A fixed DS for a signed zone."""
        self._check()
        if fqdn(zone) not in self.dnssec:
            return []
        return [f"{fqdn(zone)} IN DS 12345 13 2 " + "ab" * 32]
