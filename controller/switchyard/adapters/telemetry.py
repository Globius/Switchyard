"""Metrics (Prometheus) and access logs (Loki) of virtual services.

The contract with the telemetry pipeline (documented in ``docs/operating.md``):

- Prometheus scrapes each SE's HAProxy exporter with the labels ``tenant`` (tenant id) and the
  built-in ``proxy`` label (``vs_<service name>``).
- Loki holds the SE's HAProxy ``httplog`` lines with the labels ``tenant`` and ``frontend``
  (``vs_<service name>``).

Every query is built here with the tenant label as the first matcher and the service name
validated as a safe identifier, so a caller can never read another tenant's data.
:class:`HttpTelemetry` is the real client, :class:`FakeTelemetry` serves canned data to
tests, and the API answers ``available: false`` when none is configured.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import httpx

from switchyard.domain.telemetry import (
    AccessLogEntry,
    MetricPoint,
    MetricSummary,
    ServiceMetrics,
)
from switchyard.render.escape import ident


class TelemetryError(Exception):
    """The telemetry backend failed or answered something unexpected."""


class Telemetry(Protocol):
    """What the API needs from the metrics and logs backends."""

    async def metrics(
        self, tenant_id: UUID, service: str, *, window_s: int, step_s: int
    ) -> ServiceMetrics:
        """Series and headline numbers of one virtual service over the last ``window_s``."""
        ...

    async def summaries(
        self, tenant_id: UUID, services: list[str], *, window_s: int
    ) -> dict[str, MetricSummary]:
        """Headline numbers of several services (missing ones have no entry)."""
        ...

    async def logs(
        self,
        tenant_id: UUID,
        service: str,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        contains: str | None = None,
        status_class: int | None = None,
    ) -> list[AccessLogEntry]:
        """Access log lines of one service, newest first."""
        ...


# -- HAProxy httplog ------------------------------------------------------------------------------

#: ``<client>:<port> [<date>] <frontend> <backend>/<server> Tq/Tw/Tc/Tr/Tt <status> <bytes> ...
#: "<METHOD> <uri> <HTTP/x>"`` (HAProxy's default ``option httplog``).
_HTTPLOG = re.compile(
    r"(?P<client>\S+):\d+ \[(?P<date>[^\]]+)\] (?P<frontend>\S+) (?P<backend>[^/\s]+)/"
    r"(?P<server>\S+) (?P<tq>-?\d+)/(?P<tw>-?\d+)/(?P<tc>-?\d+)/(?P<tr>-?\d+)/(?P<tt>\+?-?\d+) "
    r"(?P<status>\d{3}) (?P<bytes>\d+) .*?(?:\"(?P<method>[A-Z]+) (?P<uri>\S*)[^\"]*\")?\s*$"
)


def parse_httplog(line: str, fallback: datetime | None = None) -> AccessLogEntry | None:
    """Parse one HAProxy ``httplog`` line; ``None`` for a line that is not one.

    Example:
        >>> e = parse_httplog('192.0.2.7:51514 [10/Oct/2026:08:30:00.123] vs_shop pool_web/web-1 '
        ...                   '0/0/1/12/13 200 512 - - ---- 1/1/0/0/0 0/0 "GET /a HTTP/1.1"')
        >>> (e.client_ip, e.status, e.duration_ms, e.path)
        ('192.0.2.7', 200, 13, '/a')
    """
    m = _HTTPLOG.search(line)
    if m is None:
        return None
    try:
        at = datetime.strptime(m["date"], "%d/%b/%Y:%H:%M:%S.%f").replace(tzinfo=UTC)
    except ValueError:
        if fallback is None:
            return None
        at = fallback
    return AccessLogEntry(
        at=at,
        client_ip=m["client"],
        frontend=m["frontend"],
        backend=m["backend"],
        server=m["server"],
        status=int(m["status"]),
        bytes=int(m["bytes"]),
        duration_ms=int(m["tt"].lstrip("+")),
        method=m["method"],
        path=m["uri"],
        raw=line,
    )


# -- queries --------------------------------------------------------------------------------------


def frontend_name(service: str) -> str:
    """The HAProxy frontend (and Prometheus ``proxy`` label) of a service.

    Raises:
        switchyard.render.escape.RenderError: If the name is not a safe identifier.
    """
    return "vs_" + ident(service)


def _selector(tenant_id: UUID, label: str, value: str) -> str:
    # `value` passed ident(); the tenant id is a UUID: neither can break out of the string.
    return f'{{tenant="{tenant_id}",{label}="{value}"}}'


def promql(tenant_id: UUID, service: str) -> dict[str, str]:
    """The PromQL expression of each metric (rate window 1 minute)."""
    sel = _selector(tenant_id, "proxy", frontend_name(service))
    return {
        "rps": f"sum(rate(haproxy_frontend_http_requests_total{sel}[1m]))",
        "error_5xx_ratio": (
            f'sum(rate(haproxy_frontend_http_responses_total{{tenant="{tenant_id}",'
            f'proxy="{frontend_name(service)}",code="5xx"}}[1m]))'
            f" / clamp_min(sum(rate(haproxy_frontend_http_responses_total{sel}[1m])), 1e-9)"
        ),
        "active_connections": f"sum(haproxy_frontend_current_sessions{sel})",
    }


def logql_p95(tenant_id: UUID, service: str) -> str:
    """LogQL for the 95th percentile of the total time of requests, in milliseconds."""
    sel = _selector(tenant_id, "frontend", frontend_name(service))
    return (
        f"quantile_over_time(0.95, {sel} | regexp "
        r"`(?P<tt>\d+) \d{3} \d+ ` | unwrap tt [1m])"
    )


class HttpTelemetry:
    """Prometheus and Loki over HTTP."""

    def __init__(
        self,
        prometheus_url: str,
        loki_url: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        """Create the client.

        Args:
            prometheus_url: Base URL of Prometheus (``https://prometheus.example.test``).
            loki_url: Base URL of Loki; without it logs and p95 are unavailable.
            client: Injected HTTP client (tests); built from ``timeout`` otherwise.
            timeout: Per-request timeout in seconds.
        """
        self._prom = prometheus_url.rstrip("/")
        self._loki = loki_url.rstrip("/") if loki_url else None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            body: dict[str, Any] = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelemetryError(f"{url}: {exc}") from exc
        if body.get("status") != "success":
            raise TelemetryError(f"{url}: {body.get('error', 'query failed')}")
        return body

    async def _range(
        self, base: str, path: str, query: str, start: datetime, end: datetime, step_s: int
    ) -> list[MetricPoint]:
        body = await self._get(
            base + path,
            {"query": query, "start": start.timestamp(), "end": end.timestamp(), "step": step_s},
        )
        result = body["data"]["result"]
        if not result:
            return []
        return [
            MetricPoint(t=datetime.fromtimestamp(float(t), UTC), v=float(v))
            for t, v in result[0]["values"]
            if v not in {"NaN", "+Inf", "-Inf"}
        ]

    async def metrics(
        self, tenant_id: UUID, service: str, *, window_s: int, step_s: int
    ) -> ServiceMetrics:
        """Query the four series; p95 is empty without Loki."""
        end = datetime.now(UTC)
        start = end - timedelta(seconds=window_s)
        queries = promql(tenant_id, service)
        series = {
            name: await self._range(self._prom, "/api/v1/query_range", q, start, end, step_s)
            for name, q in queries.items()
        }
        p95 = (
            await self._range(
                self._loki,
                "/loki/api/v1/query_range",
                logql_p95(tenant_id, service),
                start,
                end,
                step_s,
            )
            if self._loki
            else []
        )
        return ServiceMetrics(
            window_s=window_s,
            step_s=step_s,
            summary=MetricSummary(
                rps=_last(series["rps"]),
                p95_ms=_last(p95),
                error_5xx_ratio=_last(series["error_5xx_ratio"]),
                active_connections=_last(series["active_connections"]),
            ),
            rps=series["rps"],
            p95_ms=p95,
            error_5xx_ratio=series["error_5xx_ratio"],
            active_connections=series["active_connections"],
        )

    async def summaries(
        self, tenant_id: UUID, services: list[str], *, window_s: int
    ) -> dict[str, MetricSummary]:
        """Headline numbers per service (one instant query per metric and service)."""
        out: dict[str, MetricSummary] = {}
        for name in services:
            values: dict[str, float | None] = {}
            for metric, query in promql(tenant_id, name).items():
                body = await self._get(self._prom + "/api/v1/query", {"query": query})
                result = body["data"]["result"]
                values[metric] = float(result[0]["value"][1]) if result else None
            out[name] = MetricSummary(**values, p95_ms=None)
        return out

    async def logs(
        self,
        tenant_id: UUID,
        service: str,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        contains: str | None = None,
        status_class: int | None = None,
    ) -> list[AccessLogEntry]:
        """Query Loki, newest first; filters run in LogQL after the tenant matcher."""
        if self._loki is None:
            raise TelemetryError("no log backend is configured")
        query = "{" + f'tenant="{tenant_id}",frontend="{frontend_name(service)}"' + "}"
        if contains:
            query += " |= `" + contains.replace("`", "") + "`"
        if status_class is not None:
            query += f" |~ ` {status_class}\\d\\d \\d+ `"
        body = await self._get(
            self._loki + "/loki/api/v1/query_range",
            {
                "query": query,
                "start": int(since.timestamp() * 1e9),
                "end": int(until.timestamp() * 1e9),
                "limit": limit,
                "direction": "backward",
            },
        )
        entries: list[AccessLogEntry] = []
        for stream in body["data"]["result"]:
            for ns, line in stream["values"]:
                at = datetime.fromtimestamp(int(ns) / 1e9, UTC)
                parsed = parse_httplog(line, fallback=at)
                if parsed is not None:
                    entries.append(parsed)
        return sorted(entries, key=lambda e: e.at, reverse=True)[:limit]


def _last(points: list[MetricPoint]) -> float | None:
    return points[-1].v if points else None


class FakeTelemetry:
    """Canned data for tests; records the tenant ids it was asked for."""

    def __init__(self) -> None:
        """Create an empty fake."""
        self.asked: list[UUID] = []
        self.entries: list[AccessLogEntry] = []

    async def metrics(
        self, tenant_id: UUID, service: str, *, window_s: int, step_s: int
    ) -> ServiceMetrics:
        """Two flat points per series."""
        self.asked.append(tenant_id)
        now = datetime.now(UTC)
        pts = [MetricPoint(t=now - timedelta(seconds=step_s), v=1.0), MetricPoint(t=now, v=2.0)]
        return ServiceMetrics(
            window_s=window_s,
            step_s=step_s,
            summary=MetricSummary(rps=2.0, p95_ms=12.0, error_5xx_ratio=0.0, active_connections=3),
            rps=pts,
            p95_ms=pts,
            error_5xx_ratio=pts,
            active_connections=pts,
        )

    async def summaries(
        self, tenant_id: UUID, services: list[str], *, window_s: int
    ) -> dict[str, MetricSummary]:
        """The same numbers for every service."""
        self.asked.append(tenant_id)
        return {s: MetricSummary(rps=2.0, p95_ms=12.0, error_5xx_ratio=0.0) for s in services}

    async def logs(
        self,
        tenant_id: UUID,
        service: str,
        *,
        since: datetime,
        until: datetime,
        limit: int,
        contains: str | None = None,
        status_class: int | None = None,
    ) -> list[AccessLogEntry]:
        """The stored entries inside the window, filtered like the real client."""
        self.asked.append(tenant_id)
        out = [e for e in self.entries if since <= e.at < until]
        if contains:
            out = [e for e in out if contains in e.raw]
        if status_class is not None:
            out = [e for e in out if e.status // 100 == status_class]
        return sorted(out, key=lambda e: e.at, reverse=True)[:limit]
