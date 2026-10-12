"""Patroni's REST API: the cluster view the controller shows for its own members (ADR 0051).

Only ``GET /cluster`` is used, and only to read. Patroni answers it without authentication on its
REST port, which is why the port must stay on the controllers' private network.
:class:`FakePatroni` is an in-memory answer for tests.
"""

import asyncio
import ipaddress
import socket
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

#: Seconds to wait for one member before reporting it unreachable.
TIMEOUT_S = 3.0


class PatroniError(Exception):
    """Patroni could not be asked, or answered something else than a cluster."""


class Patroni(Protocol):
    """What the controller needs from Patroni."""

    async def cluster(self, base_url: str) -> dict[str, Any]:
        """Return the JSON of ``GET {base_url}/cluster``."""
        ...


def check_target(base_url: str) -> None:
    """Raise :class:`PatroniError` if ``base_url`` is not a machine the controller may contact.

    Loopback, link-local (cloud metadata), multicast, unspecified and reserved addresses are
    refused, for every address the name resolves to. Private addresses are fine.
    """
    host = urlsplit(base_url).hostname or ""
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            found = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise PatroniError(f"cannot resolve {host}") from exc
        addresses = [ipaddress.ip_address(item[4][0]) for item in found]
    for ip in addresses:
        if (
            ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
        ):
            raise PatroniError(f"{ip} is not an address the controller may contact")


class HttpPatroni:
    """The real client."""

    def __init__(self, timeout_s: float = TIMEOUT_S) -> None:
        """Create a client with a per-call timeout."""
        self._timeout = timeout_s

    async def cluster(self, base_url: str) -> dict[str, Any]:
        """Ask one member for the cluster view."""
        await asyncio.to_thread(check_target, base_url)
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                response = await client.get(base_url.rstrip("/") + "/cluster")
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PatroniError(str(exc) or exc.__class__.__name__) from exc
        if not isinstance(payload, dict):
            raise PatroniError("not a Patroni cluster answer")
        return payload


class FakePatroni:
    """An in-memory Patroni: answers per base URL, or fails for the URLs in ``down``."""

    def __init__(self, clusters: dict[str, dict[str, Any]] | None = None) -> None:
        """Create it with the answers to give."""
        self.clusters = clusters or {}
        self.down: set[str] = set()
        self.asked: list[str] = []

    async def cluster(self, base_url: str) -> dict[str, Any]:
        """Return the configured answer or raise like an unreachable member."""
        self.asked.append(base_url)
        if base_url in self.down or base_url not in self.clusters:
            raise PatroniError("connection refused")
        return self.clusters[base_url]
