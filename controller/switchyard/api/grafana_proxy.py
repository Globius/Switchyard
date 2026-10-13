# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Reverse proxies for the embedded Grafana (ADR 0085).

Two proxies live here:

- :meth:`GrafanaProxy.forward` carries a browser's requests to Grafana. It authenticates the
  *controller's* proxy cookie, throws away everything the browser sent that Grafana could treat as
  credentials, and adds the trusted ``X-WEBAUTH-USER`` / ``X-Grafana-Org-Id`` headers itself.
- :meth:`GrafanaProxy.datasource` carries Grafana's data source queries to Prometheus and Loki after
  :mod:`switchyard.domain.query_scope` has scoped them to the tenant named by the data source token.
  This is the tenant boundary: nothing Grafana is asked to do changes which tenant it can read.
"""

from urllib.parse import parse_qsl, urlencode

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from switchyard.api.problems import ApiError
from switchyard.domain.query_scope import (
    Params,
    ScopeError,
    scope_loki_request,
    scope_prometheus_request,
)
from switchyard.services.grafana import GrafanaService, GrantInfo

#: Largest request body that is forwarded (Grafana queries and preferences are tiny).
MAX_BODY = 4 * 1024 * 1024
PROXY_COOKIE = "sy_grafana"

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
#: Never forwarded from the browser: credentials Grafana might honor, and anything that claims to
#: come from a trusted proxy.
_REQUEST_DROP = _HOP_BY_HOP | {
    "cookie",
    "authorization",
    "content-length",
    "forwarded",
    "x-real-ip",
    "x-csrf-token",
}
_REQUEST_DROP_PREFIXES = ("x-webauth", "x-grafana", "x-forwarded", "x-scope")
_RESPONSE_DROP = _HOP_BY_HOP | {"set-cookie", "set-cookie2"}

#: Grafana paths that manage Grafana itself. A Viewer cannot use them anyway; refusing them here
#: keeps that true if a role is ever wrong.
_DENIED_PREFIXES = (
    "api/admin",
    "api/orgs",
    "api/serviceaccounts",
    "api/auth",
    "api/users",
    "api/teams",
    "api/snapshots",
    "api/live",
    "api/cloudmigration",
    "api/license",
    "logout",
)


def _denied(path: str) -> bool:
    path = path.strip("/").lower()
    return any(path == p or path.startswith(p + "/") for p in _DENIED_PREFIXES)


def _clean_path(path: str) -> str:
    """Refuse paths that Grafana could read differently from the deny list.

    The path is percent-decoded before it gets here, so ``%3F`` arrives as ``?``. Left in, it turns
    ``api/users?`` into a request for ``/api/users`` that the deny list (whole segments) does not
    recognise. Dot segments, empty segments, ``?``, ``#``, ``;``, backslashes and control
    characters are refused for the same reason.

    Example:
        >>> _clean_path("d/abc/slug")
        'd/abc/slug'
    """
    segments = path.split("/")
    if any(s in {".", ".."} or any(c in s for c in "\\?#;") for s in segments):
        raise ApiError(400, "invalid path")
    if any(not s for s in segments[:-1]) or any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        raise ApiError(400, "invalid path")
    return path


class GrafanaProxy:
    """Forwards browser and data source requests; owns the HTTP client."""

    def __init__(self, service: GrafanaService, client: httpx.AsyncClient | None = None) -> None:
        """Create the proxy; ``client`` is injected by tests."""
        self._service = service
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    async def _send(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> Response:
        try:
            upstream = await self._client.send(
                self._client.build_request(method, url, headers=headers, content=body),
                stream=True,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise ApiError(502, f"upstream unavailable: {type(exc).__name__}") from exc
        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _RESPONSE_DROP}
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=out_headers,
            background=BackgroundTask(upstream.aclose),
        )

    async def _body(self, request: Request) -> bytes | None:
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return None
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY:
            raise ApiError(413, "request body too large")
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                raise ApiError(413, "request body too large")
        return body

    # -- browser -> Grafana -----------------------------------------------------------------------

    async def forward(self, request: Request, path: str, grant: GrantInfo) -> Response:
        """Send one browser request to Grafana as the grant's user, in the grant's organization."""
        path = _clean_path(path)
        if _denied(path):
            raise ApiError(403, "this part of Grafana is not available through Switchyard")
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _REQUEST_DROP and not k.lower().startswith(_REQUEST_DROP_PREFIXES)
        }
        # Grafana compares Origin with Host on writes, so it must see the public host.
        headers["host"] = request.headers.get("host", "")
        headers["X-WEBAUTH-USER"] = grant.login
        headers["X-Grafana-Org-Id"] = str(grant.org_id)
        base = self._service.config.internal_url.rstrip("/")
        url = f"{base}/{path}"
        if request.url.query:
            url += "?" + request.url.query
        response = await self._send(request.method, url, headers, await self._body(request))
        location = response.headers.get("location")
        if location and location.startswith(base):
            response.headers["location"] = "/grafana" + location[len(base) :]
        return response

    # -- Grafana -> Prometheus / Loki -------------------------------------------------------------

    async def datasource(self, request: Request, kind: str, path: str) -> Response:
        """Scope one data source query to the tenant of its token, then forward it.

        Raises:
            ApiError: 401 for a missing or foreign token, 403 for a query or endpoint that cannot be
                scoped, 404 for a backend that is not configured.
        """
        auth = request.headers.get("authorization", "")
        tenant_id = (
            self._service.tenant_for_ds_token(auth[7:].strip())
            if auth.lower().startswith("bearer ")
            else None
        )
        if tenant_id is None:
            raise ApiError(401, "invalid data source token", code="invalid_token")
        config = self._service.config
        # Grafana appends the backend's own API path to the data source URL, so the path here is
        # `api/v1/query_range` (Prometheus) or `loki/api/v1/query_range` (Loki).
        if kind == "prometheus":
            base, prefix, scope = config.prometheus_url, "api/v1/", scope_prometheus_request
        elif kind == "loki" and config.loki_url:
            base, prefix, scope = config.loki_url, "loki/api/v1/", scope_loki_request
        else:
            raise ApiError(404, f"no {kind} backend is configured")
        path = path.strip("/")
        if not path.startswith(prefix):
            raise ApiError(403, "query refused: unknown endpoint", code="query_refused")
        path = path[len(prefix) :]
        if request.method not in {"GET", "POST"}:
            raise ApiError(405, "only GET and POST are allowed")
        params: Params = parse_qsl(request.url.query, keep_blank_values=True)
        body: bytes | None = None
        if request.method == "POST":
            if not request.headers.get("content-type", "").startswith(
                "application/x-www-form-urlencoded"
            ):
                raise ApiError(415, "data source queries are sent as a URL-encoded form")
            raw = (await self._body(request)) or b""
            params += parse_qsl(raw.decode("utf-8", "strict"), keep_blank_values=True)
        try:
            scoped = scope(_clean_path(path), params, tenant_id)
        except (ScopeError, UnicodeDecodeError) as exc:
            raise ApiError(403, f"query refused: {exc}", code="query_refused") from exc
        url = base.rstrip("/") + "/" + prefix + path
        encoded = urlencode(scoped)
        headers = {"accept": request.headers.get("accept", "application/json")}
        if request.method == "POST":
            body = encoded.encode()
            headers["content-type"] = "application/x-www-form-urlencoded"
        elif encoded:
            url += "?" + encoded
        return await self._send(request.method, url, headers, body)
