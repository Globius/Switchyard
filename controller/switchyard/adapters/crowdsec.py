# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""A CrowdSec local API (LAPI), read as a bouncer (ADR 0213).

The controller registers nothing on the CrowdSec side; the operator runs ``cscli bouncers add`` and
gives Switchyard the key (as an OpenBao reference). The controller then reads the decision stream
(``GET /v1/decisions/stream?startup=true``, header ``X-Api-Key``) and turns the ban decisions on
addresses and ranges into the networks of a reputation list.

Two implementations share :class:`CrowdSec`: :class:`HttpCrowdSec` and :class:`FakeCrowdSec`.

Example:
    >>> parse_decisions('{"new":[{"scope":"Ip","value":"192.0.2.9","type":"ban"}]}')
    ['192.0.2.9/32']
"""

import json
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from switchyard.adapters.iplists import IpListError, parse_networks

#: Decision scopes that name addresses; decisions on users, countries or ASNs are skipped.
_SCOPES = frozenset({"ip", "range"})
_MAX_BYTES = 64 * 1024 * 1024


class CrowdSecError(IpListError):
    """The LAPI could not be reached, refused the key, or answered with something unexpected."""


def parse_decisions(body: str, origins: frozenset[str] | None = None) -> list[str]:
    """Read the ban decisions of a ``/v1/decisions/stream?startup=true`` answer.

    Only ``ban`` decisions on an ``Ip`` or ``Range`` scope count; captcha and throttle decisions and
    decisions on countries, ASNs or users are skipped. ``new: null`` is an empty list.

    Args:
        body: The JSON answer.
        origins: Keep only decisions from these origins (``crowdsec``, ``CAPI``, ``cscli``,
            ``lists``); ``None`` keeps all.

    Raises:
        CrowdSecError: If the body is not a decision stream, or the list is too long.

    Example:
        >>> parse_decisions('{"new":[{"scope":"Range","value":"198.51.100.0/24","type":"ban"}]}')
        ['198.51.100.0/24']
    """
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise CrowdSecError("the answer is not JSON") from exc
    if not isinstance(data, dict):
        raise CrowdSecError("the answer is not a CrowdSec decision stream")
    new = data.get("new") or []
    if not isinstance(new, list):
        raise CrowdSecError("the answer is not a CrowdSec decision stream")
    lines: list[str] = []
    for decision in new:
        if not isinstance(decision, dict):
            raise CrowdSecError("the answer is not a CrowdSec decision stream")
        if str(decision.get("type", "")).lower() != "ban":
            continue
        if str(decision.get("scope", "")).lower() not in _SCOPES:
            continue
        if origins is not None and decision.get("origin") not in origins:
            continue
        lines.append(str(decision.get("value", "")))
    try:
        return parse_networks("\n".join(lines))
    except IpListError as exc:
        raise CrowdSecError(str(exc)) from exc


def check_url(url: str) -> str:
    """The URL without a trailing slash, if it is http(s) with a host and no credentials.

    Raises:
        ValueError: Otherwise.

    Example:
        >>> check_url("https://crowdsec.example.test:8080/")
        'https://crowdsec.example.test:8080'
    """
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise ValueError("must be an http(s) URL without credentials, query or fragment")
    return url.rstrip("/")


class CrowdSec(Protocol):
    """A CrowdSec local API."""

    async def decisions(
        self, url: str, key: str, origins: frozenset[str] | None = None
    ) -> list[str]:
        """The networks currently banned (see :func:`parse_decisions`)."""
        ...


class HttpCrowdSec:
    """The decision stream read over HTTP(S); redirects are not followed."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        """``transport`` is replaced in tests."""
        self._http = httpx.AsyncClient(timeout=30.0, transport=transport, follow_redirects=False)

    async def decisions(
        self, url: str, key: str, origins: frozenset[str] | None = None
    ) -> list[str]:
        """Fetch the stream with the bouncer ``key`` and parse the bans.

        Raises:
            CrowdSecError: On a transport error, a status other than 200 (``403`` means the key
                was refused), an oversized answer, or an answer that is not a decision stream.
        """
        try:
            async with self._http.stream(
                "GET",
                f"{check_url(url)}/v1/decisions/stream",
                params={"startup": "true"},
                headers={"X-Api-Key": key},
            ) as response:
                if response.status_code != 200:
                    raise CrowdSecError(f"the CrowdSec API answered HTTP {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > _MAX_BYTES:
                        raise CrowdSecError("the CrowdSec answer is larger than 64 MiB")
        except httpx.HTTPError as exc:
            raise CrowdSecError(
                f"the CrowdSec API could not be reached ({type(exc).__name__})"
            ) from exc
        except ValueError as exc:
            raise CrowdSecError(f"the CrowdSec URL is not usable: {exc}") from exc
        return parse_decisions(body.decode("utf-8", errors="replace"), origins)

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()


@dataclass
class FakeCrowdSec:
    """In-memory LAPI for tests.

    Attributes:
        banned: Networks to answer with.
        key: The key that is accepted.
        calls: Every call as ``(url, key, origins)``.
    """

    banned: list[str] = field(default_factory=list)
    key: str = "bouncer-key"
    calls: list[tuple[str, str, frozenset[str] | None]] = field(default_factory=list)

    async def decisions(
        self, url: str, key: str, origins: frozenset[str] | None = None
    ) -> list[str]:
        """Return the banned networks, or fail like a refused key."""
        self.calls.append((url, key, origins))
        if key != self.key:
            raise CrowdSecError("the CrowdSec API answered HTTP 403")
        return list(self.banned)
