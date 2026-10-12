"""Google Cloud DNS: TXT record sets through the REST API, with a service-account key.

The access token comes from a self-signed JWT exchanged at Google's token endpoint. A record set
holds every value of a name, so a change deletes the old set and adds the new one in one call.
"""

import base64
import json
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from switchyard.adapters.dns.base import DnsError
from switchyard.adapters.dns.zones import in_zone

SCOPE = "https://www.googleapis.com/auth/ndev.clouddns.readwrite"
API = "https://dns.googleapis.com/dns/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_jwt(account: dict[str, Any], *, now: float, scope: str = SCOPE) -> str:
    """The signed assertion for a service-account key (RS256).

    Example:
        >>> from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
        >>> key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
        >>> pem = key.private_bytes(
        ...     serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        ...     serialization.NoEncryption()).decode()
        >>> jwt = sign_jwt({"client_email": "sa@p.iam", "private_key": pem}, now=1000)
        >>> len(jwt.split("."))
        3
    """
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(
        json.dumps(
            {
                "iss": account["client_email"],
                "scope": scope,
                "aud": TOKEN_URL,
                "iat": int(now),
                "exp": int(now) + 3600,
            }
        ).encode()
    )
    key = serialization.load_pem_private_key(account["private_key"].encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise DnsError("the service account key is not an RSA key")
    signature = key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64(signature)}"


class GoogleDns:
    """Google Cloud DNS TXT records for ACME DNS-01."""

    def __init__(
        self,
        service_account_json: str,
        project_id: str,
        managed_zone: str,
        zone: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create the client for one managed zone.

        Args:
            service_account_json: The key file of a service account that may edit the zone.
            project_id: The Google Cloud project.
            managed_zone: The managed zone's resource name.
            zone: The DNS name of the zone, for example ``example.test``.
            transport: Replaceable HTTP transport (tests).
        """
        try:
            self._account: dict[str, Any] = json.loads(service_account_json)
            self._account["client_email"], self._account["private_key"]
        except (ValueError, KeyError, TypeError) as exc:
            raise DnsError(
                "the service account key must be the JSON file with client_email and private_key"
            ) from exc
        self._base = f"{API}/projects/{project_id}/managedZones/{managed_zone}"
        self._zone = zone.strip(".")
        self._http = httpx.AsyncClient(transport=transport, timeout=30.0)
        self._token: str | None = None
        self._expires = 0.0

    async def _bearer(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        assertion = sign_jwt(self._account, now=time.time())
        # never the key file's token_uri: a tenant could point it at an internal service
        url = TOKEN_URL
        try:
            response = await self._http.post(
                url,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
        except httpx.HTTPError as exc:
            raise DnsError(f"Google sign-in: {exc}") from exc
        if response.status_code != 200:
            raise DnsError(f"Google sign-in: HTTP {response.status_code}: {_message(response)}")
        body = response.json()
        self._token = str(body["access_token"])
        self._expires = time.time() + float(body.get("expires_in", 3000))
        return self._token

    async def _call(
        self, method: str, path: str, *, params: dict[str, str] | None = None, body: Any = None
    ) -> Any:
        try:
            response = await self._http.request(
                method,
                f"{self._base}{path}",
                params=params,
                json=body,
                headers={"Authorization": f"Bearer {await self._bearer()}"},
            )
        except httpx.HTTPError as exc:
            raise DnsError(f"Google DNS {method} {path}: {exc}") from exc
        if response.status_code >= 400:
            raise DnsError(
                f"Google DNS {method} {path}: HTTP {response.status_code}: {_message(response)}"
            )
        return response.json() if response.content else {}

    async def _current(self, name: str) -> dict[str, Any] | None:
        found = await self._call("GET", "/rrsets", params={"name": f"{name}.", "type": "TXT"})
        for rrset in found.get("rrsets", []):
            if rrset.get("type") == "TXT" and rrset.get("name", "").rstrip(".") == name:
                return dict(rrset)
        return None

    async def _change(
        self, name: str, old: dict[str, Any] | None, values: list[str], ttl: int
    ) -> None:
        body: dict[str, Any] = {}
        if old is not None:
            body["deletions"] = [old]
        if values:
            body["additions"] = [{"name": f"{name}.", "type": "TXT", "ttl": ttl, "rrdatas": values}]
        if body:
            await self._call("POST", "/changes", body=body)

    @staticmethod
    def _quote(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    async def create_txt(self, name: str, value: str, *, ttl: int = 60) -> str:
        """Add ``value`` to the TXT set of ``name``."""
        if not in_zone(name, self._zone):
            raise DnsError(f"{name} is not inside the Google zone {self._zone}")
        old = await self._current(name)
        values = list(old.get("rrdatas", [])) if old else []
        quoted = self._quote(value)
        if quoted not in values:
            await self._change(
                name, old, [*values, quoted], int(old["ttl"]) if old else max(ttl, 60)
            )
        return value

    async def delete_txt(self, name: str, handle: str) -> None:
        """Remove ``handle`` (the value) from the TXT set of ``name``."""
        old = await self._current(name)
        quoted = self._quote(handle)
        if old is None or quoted not in old.get("rrdatas", []):
            return
        remaining = [v for v in old["rrdatas"] if v != quoted]
        await self._change(name, old, remaining, int(old["ttl"]))

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
        return str(error.get("message") or error)[:300]
    return str(body.get("error_description") or error or body)[:300]
