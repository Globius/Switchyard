# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Country and reputation network lists, fetched from sources the operator configures (ADR 0147).

Tenants name a country code or a feed name; they never supply a URL, so a tenant cannot make the
controller fetch an address of its choosing. The operator points the controller at the sources:

- ``SWITCHYARD_GEO_URL``: a URL template with ``{cc}`` (lower-case country code) that answers with
  one network per line, for example ``https://www.ipdeny.com/ipblocks/data/aggregated/{cc}-aggregated.zone``
  or a mirror of MaxMind data the operator publishes in that format.
- ``SWITCHYARD_REPUTATION_FEEDS``: JSON ``{"name": "url"}`` of plain-text feeds (Spamhaus DROP,
  a CrowdSec or other exporter, a blocklist mirror).

- ``SWITCHYARD_CROWDSEC_URL`` and ``SWITCHYARD_CROWDSEC_API_KEY``: the local API of a CrowdSec
  security engine and the key of a bouncer registered on it (ADR 0213). The decisions appear as the
  reputation feed named :data:`CROWDSEC_FEED`.

Two implementations share :class:`IpLists`: :class:`HttpIpLists` and :class:`FakeIpLists`.

Example:
    >>> parse_networks("192.0.2.0/24 ; SBL1")
    ['192.0.2.0/24']
"""

import ipaddress
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import httpx

#: Longest list one source may deliver; a longer answer is refused, not truncated.
MAX_NETWORKS = 100_000
_MAX_BYTES = 16 * 1024 * 1024
_COMMENT = re.compile(r"[;#].*$")
#: Name of the reputation feed that is backed by a CrowdSec local API.
CROWDSEC_FEED = "crowdsec"
#: Decision scopes that name addresses; decisions on users, countries or ASNs are skipped.
_CROWDSEC_SCOPES = frozenset({"ip", "range"})


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


def parse_crowdsec_decisions(body: str, origins: frozenset[str] | None = None) -> list[str]:
    """Read the ban decisions of a CrowdSec ``/v1/decisions/stream?startup=true`` answer.

    Only ``ban`` decisions on an ``Ip`` or ``Range`` scope count; captcha and throttle decisions and
    decisions on countries, ASNs or users are skipped. An empty ``new`` list (or ``null``) is an
    empty list, which the sync job treats like any other empty answer.

    Args:
        body: The JSON answer.
        origins: Keep only decisions from these origins (``crowdsec``, ``CAPI``, ``cscli``,
            ``lists``); ``None`` keeps all.

    Raises:
        IpListError: If the body is not the expected JSON, or the list is longer than
            :data:`MAX_NETWORKS`.

    Example:
        >>> parse_crowdsec_decisions('{"new":[{"scope":"Ip","value":"192.0.2.9","type":"ban"}]}')
        ['192.0.2.9/32']
    """
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise IpListError("the answer is not JSON") from exc
    new = data.get("new") if isinstance(data, dict) else None
    if new is None and isinstance(data, dict):
        new = []
    if not isinstance(new, list):
        raise IpListError("the answer is not a CrowdSec decision stream")
    lines: list[str] = []
    for decision in new:
        if not isinstance(decision, dict):
            raise IpListError("the answer is not a CrowdSec decision stream")
        if str(decision.get("type", "")).lower() != "ban":
            continue
        if str(decision.get("scope", "")).lower() not in _CROWDSEC_SCOPES:
            continue
        if origins is not None and decision.get("origin") not in origins:
            continue
        lines.append(str(decision.get("value", "")))
    return parse_networks("\n".join(lines))


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
        crowdsec_url: str | None = None,
        crowdsec_key: str | None = None,
        crowdsec_origins: frozenset[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Create the source.

        Args:
            geo_url: Template with ``{cc}``; ``None`` turns country lists off.
            feeds: Feed name to URL.
            crowdsec_url: Base URL of a CrowdSec local API; adds the feed :data:`CROWDSEC_FEED`.
            crowdsec_key: The bouncer API key for it.
            crowdsec_origins: Keep only decisions from these origins; ``None`` keeps all.
            transport: For tests.

        Raises:
            ValueError: If a URL is not http(s), the template has no ``{cc}``, the CrowdSec URL
                comes without a key, or a configured feed uses the reserved name ``crowdsec``.
        """
        if geo_url is not None and "{cc}" not in geo_url:
            raise ValueError("the geo URL needs a {cc} placeholder")
        if crowdsec_url and not crowdsec_key:
            raise ValueError("the CrowdSec URL needs a bouncer API key")
        if crowdsec_url and CROWDSEC_FEED in (feeds or {}):
            raise ValueError(f"the feed name {CROWDSEC_FEED!r} is reserved for the CrowdSec source")
        urls = [*(feeds or {}).values(), *([geo_url] if geo_url else [])]
        urls += [crowdsec_url] if crowdsec_url else []
        for url in urls:
            if not url.startswith(("https://", "http://")):
                raise ValueError(f"list URLs must be http(s): {url!r}")
        self._geo = geo_url
        self._feeds = dict(feeds or {})
        self._crowdsec = crowdsec_url.rstrip("/") if crowdsec_url else None
        self._crowdsec_key = crowdsec_key
        self._crowdsec_origins = crowdsec_origins
        self._http = httpx.AsyncClient(timeout=30.0, transport=transport, follow_redirects=False)

    def feeds(self) -> frozenset[str]:
        """Names of the configured feeds, plus ``crowdsec`` when a local API is configured."""
        return frozenset(self._feeds) | ({CROWDSEC_FEED} if self._crowdsec else frozenset())

    def has_geo(self) -> bool:
        """True when a country URL template is configured."""
        return self._geo is not None

    async def _get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        parse: Callable[[str], list[str]] = parse_networks,
    ) -> list[str]:
        try:
            async with self._http.stream("GET", url, headers=headers) as response:
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
            return parse(body.decode("utf-8", errors="replace"))
        except IpListError as exc:
            raise IpListError(f"{url}: {exc}") from exc

    async def country(self, code: str) -> list[str]:
        """Fetch one country's networks."""
        if self._geo is None:
            raise IpListError("country lists are not configured (SWITCHYARD_GEO_URL)")
        return await self._get(self._geo.replace("{cc}", code.lower()))

    async def feed(self, name: str) -> list[str]:
        """Fetch one feed."""
        if name == CROWDSEC_FEED and self._crowdsec:
            origins = self._crowdsec_origins
            return await self._get(
                f"{self._crowdsec}/v1/decisions/stream?startup=true",
                headers={"X-Api-Key": self._crowdsec_key or ""},
                parse=lambda text: parse_crowdsec_decisions(text, origins),
            )
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
