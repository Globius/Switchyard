"""Country and reputation network lists, fetched from sources the operator configures (ADR 0147).

Tenants name a country code or a feed name; they never supply a URL, so a tenant cannot make the
controller fetch an address of its choosing. The operator points the controller at the sources:

- ``SWITCHYARD_GEO_URL``: a URL template with ``{cc}`` (lower-case country code) that answers with
  one network per line, for example ``https://www.ipdeny.com/ipblocks/data/aggregated/{cc}-aggregated.zone``
  or a mirror of MaxMind data the operator publishes in that format.
- ``SWITCHYARD_REPUTATION_FEEDS``: JSON ``{"name": "url"}`` of plain-text feeds (Spamhaus DROP,
  a CrowdSec or other exporter, a blocklist mirror).

Two implementations share :class:`IpLists`: :class:`HttpIpLists` and :class:`FakeIpLists`.

Example:
    >>> parse_networks("192.0.2.0/24 ; SBL1")
    ['192.0.2.0/24']
"""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx

#: Longest list one source may deliver; a longer answer is refused, not truncated.
MAX_NETWORKS = 100_000
_MAX_BYTES = 16 * 1024 * 1024
_COMMENT = re.compile(r"[;#].*$")


class IpListError(Exception):
    """A list could not be fetched or is not a list of networks."""


def parse_networks(text: str) -> list[str]:
    """Read one network (or address) per line; ``#`` and ``;`` start a comment.

    A source that returns an HTML error page or any other text is refused: if more than one line
    in five is not a network, the whole answer is rejected.

    Raises:
        IpListError: If the text is mostly not networks, or longer than :data:`MAX_NETWORKS`.

    Example:
        >>> parse_networks("2001:db8::/32")
        ['2001:db8::/32']
        >>> parse_networks("<html>")
        Traceback (most recent call last):
            ...
        switchyard.adapters.iplists.IpListError: the answer is not a list of networks
    """
    networks: set[ipaddress.IPv4Network | ipaddress.IPv6Network] = set()
    lines = bad = 0
    for raw in text.splitlines():
        line = _COMMENT.sub("", raw).strip()
        if not line:
            continue
        lines += 1
        try:
            networks.add(ipaddress.ip_network(line.split()[0], strict=False))
        except ValueError:
            bad += 1
    if lines and bad * 5 > lines:
        raise IpListError("the answer is not a list of networks")
    if len(networks) > MAX_NETWORKS:
        raise IpListError(f"the list has {len(networks)} networks; the limit is {MAX_NETWORKS}")
    return sorted(
        (str(n) for n in networks),
        key=lambda s: (":" in s, ipaddress.ip_network(s).network_address),
    )


class IpLists(Protocol):
    """A source of country and reputation lists."""

    def feeds(self) -> frozenset[str]:
        """Names of the reputation feeds this source offers."""
        ...

    def has_geo(self) -> bool:
        """True when country lists are available."""
        ...

    async def country(self, code: str) -> list[str]:
        """Networks of one country (an upper-case ISO code)."""
        ...

    async def feed(self, name: str) -> list[str]:
        """Networks of one reputation feed."""
        ...


class HttpIpLists:
    """Lists fetched over HTTP(S) from operator-configured URLs.

    Example:
        >>> HttpIpLists("https://lists.example.test/{cc}.zone", {"drop": "https://x.test/drop"}).feeds()
        frozenset({'drop'})
    """

    def __init__(
        self,
        geo_url: str | None = None,
        feeds: dict[str, str] | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create the source.

        Args:
            geo_url: Template with ``{cc}``; ``None`` turns country lists off.
            feeds: Feed name to URL.
            transport: For tests.

        Raises:
            ValueError: If a URL is not http(s) or the template has no ``{cc}``.
        """
        if geo_url is not None and "{cc}" not in geo_url:
            raise ValueError("the geo URL needs a {cc} placeholder")
        for url in [*(feeds or {}).values(), *([geo_url] if geo_url else [])]:
            if not url.startswith(("https://", "http://")):
                raise ValueError(f"list URLs must be http(s): {url!r}")
        self._geo = geo_url
        self._feeds = dict(feeds or {})
        self._http = httpx.AsyncClient(timeout=30.0, transport=transport, follow_redirects=False)

    def feeds(self) -> frozenset[str]:
        """Names of the configured feeds."""
        return frozenset(self._feeds)

    def has_geo(self) -> bool:
        """True when a country URL template is configured."""
        return self._geo is not None

    async def _get(self, url: str) -> list[str]:
        try:
            async with self._http.stream("GET", url) as response:
                if response.status_code != 200:
                    raise IpListError(f"{url}: HTTP {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > _MAX_BYTES:
                        raise IpListError(f"{url}: the answer is larger than 16 MiB")
        except httpx.HTTPError as exc:
            raise IpListError(f"{url}: {exc}") from exc
        try:
            return parse_networks(body.decode("utf-8", errors="replace"))
        except IpListError as exc:
            raise IpListError(f"{url}: {exc}") from exc

    async def country(self, code: str) -> list[str]:
        """Fetch one country's networks."""
        if self._geo is None:
            raise IpListError("country lists are not configured (SWITCHYARD_GEO_URL)")
        return await self._get(self._geo.replace("{cc}", code.lower()))

    async def feed(self, name: str) -> list[str]:
        """Fetch one feed."""
        if name not in self._feeds:
            raise IpListError(f"feed {name!r} is not configured")
        return await self._get(self._feeds[name])

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()


@dataclass
class FakeIpLists:
    """In-memory lists for tests.

    Attributes:
        countries: Upper-case code to networks.
        feed_lists: Feed name to networks.
        calls: Every fetch as ``(kind, name)``.
        broken: Names (code or feed) whose fetch raises :class:`IpListError`.
    """

    countries: dict[str, list[str]] = field(default_factory=dict)
    feed_lists: dict[str, list[str]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    broken: set[str] = field(default_factory=set)

    def feeds(self) -> frozenset[str]:
        """Names of the feeds."""
        return frozenset(self.feed_lists)

    def has_geo(self) -> bool:
        """Always true."""
        return True

    async def country(self, code: str) -> list[str]:
        """Return the configured networks."""
        self.calls.append(("country", code))
        if code in self.broken:
            raise IpListError(f"{code}: unreachable")
        return list(self.countries.get(code, []))

    async def feed(self, name: str) -> list[str]:
        """Return the configured networks."""
        self.calls.append(("feed", name))
        if name in self.broken:
            raise IpListError(f"{name}: unreachable")
        if name not in self.feed_lists:
            raise IpListError(f"feed {name!r} is not configured")
        return list(self.feed_lists[name])
