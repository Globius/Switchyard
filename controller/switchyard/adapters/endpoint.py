# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""HTTP request hooks that connect only to a policy-checked address.

The original Host header and TLS server name are retained. Both direct connections and
configured HTTP proxies receive a literal destination, preventing a second DNS lookup.
"""

import asyncio

import httpx

from switchyard.adapters.dns.base import resolve_endpoint


async def pin_request(request: httpx.Request, *, what: str, env: str) -> None:
    """Pin a request to one allowed IP while preserving hostname certificate verification.

    Raises:
        httpx.ConnectError: If the address policy rejects any DNS answer or resolution fails.
    """
    host = request.url.host
    try:
        addresses = await asyncio.to_thread(resolve_endpoint, host, what, env=env)
    except ValueError as exc:
        raise httpx.ConnectError(str(exc), request=request) from exc
    request.headers["Host"] = request.url.netloc.decode("ascii")
    request.extensions["sni_hostname"] = host
    request.url = request.url.copy_with(host=addresses[0])
