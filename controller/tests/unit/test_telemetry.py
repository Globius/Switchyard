# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""The Prometheus/Loki adapter: tenant-scoped queries and httplog parsing."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from switchyard.adapters.telemetry import (
    HttpTelemetry,
    TelemetryError,
    engine_promql,
    frontend_name,
    parse_httplog,
    promql,
)
from switchyard.render.escape import RenderError

TENANT = uuid.UUID(int=7)
LINE = (
    "192.0.2.7:51514 [10/Oct/2026:08:30:00.123] vs_shop pool_web/web-1 0/0/1/12/13 200 512 - - "
    '---- 1/1/0/0/0 0/0 "GET /cart?x=1 HTTP/1.1"'
)


def test_httplog_is_parsed() -> None:
    entry = parse_httplog(LINE)
    assert entry is not None
    assert (entry.client_ip, entry.frontend, entry.backend, entry.server) == (
        "192.0.2.7",
        "vs_shop",
        "pool_web",
        "web-1",
    )
    assert (entry.status, entry.bytes, entry.duration_ms) == (200, 512, 13)
    assert (entry.method, entry.path) == ("GET", "/cart?x=1")
    assert entry.at == datetime(2026, 10, 10, 8, 30, 0, 123000, tzinfo=UTC)
    assert parse_httplog("not a log line") is None


def test_every_query_carries_the_tenant_label_and_refuses_unsafe_names() -> None:
    for query in promql(TENANT, "shop").values():
        assert f'tenant="{TENANT}"' in query
        assert 'proxy="vs_shop"' in query
    assert frontend_name("shop") == "vs_shop"
    with pytest.raises(RenderError):
        promql(TENANT, 'x"} or {tenant="other')


def test_engine_queries_carry_tenant_first_and_refuse_unsafe_names() -> None:
    queries = engine_promql(TENANT, "se-a-01")
    assert set(queries) == {
        "cpu_percent",
        "memory_used_bytes",
        "haproxy_sessions",
        "traffic_in_bps",
        "vcpus",
        "memory_total_bytes",
    }
    for query in queries.values():
        # every selector starts with the tenant, then the engine
        assert query.count("{") == query.count(f'{{tenant="{TENANT}",engine="se-a-01"')
    with pytest.raises(RenderError):
        engine_promql(TENANT, 'x"} or {tenant="other')


async def test_engine_metrics_are_queried_per_tenant_and_engine_at_the_end_of_the_range() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/v1/query":
            return httpx.Response(
                200,
                json={"status": "success", "data": {"result": [{"value": [1760000000, "4"]}]}},
            )
        if "node_cpu_seconds_total" in request.url.params["query"]:
            return httpx.Response(200, json={"status": "success", "data": {"result": []}})
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"result": [{"values": [[1760000000, "7"], [1760000030, "NaN"]]}]},
            },
        )

    t = HttpTelemetry(
        "http://prom", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    end = datetime(2026, 10, 10, 8, 30, tzinfo=UTC)
    m = await t.engine_metrics(TENANT, "se-a-01", window_s=3600, step_s=30, until=end)
    assert m.cpu_percent == [] and m.summary.cpu_percent is None  # no data is not zero
    assert [p.v for p in m.haproxy_sessions] == [7.0]  # NaN dropped: a gap
    assert m.summary.vcpus == 4 and m.summary.memory_total_bytes == 4
    assert all(f'tenant="{TENANT}",engine="se-a-01"' in r.url.params["query"] for r in seen)
    ranges = [r for r in seen if r.url.path == "/api/v1/query_range"]
    assert {float(r.url.params["end"]) for r in ranges} == {end.timestamp()}
    assert {float(r.url.params["start"]) for r in ranges} == {end.timestamp() - 3600}


async def test_the_http_client_queries_and_parses() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if (
            request.url.path == "/loki/api/v1/query_range"
            and "quantile" not in request.url.params["query"]
        ):
            ns = str(int(datetime(2026, 10, 10, 8, 30, tzinfo=UTC).timestamp() * 1e9))
            return httpx.Response(
                200,
                json={"status": "success", "data": {"result": [{"values": [[ns, LINE]]}]}},
            )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"result": [{"values": [[1760000000, "2.5"], [1760000030, "NaN"]]}]},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    t = HttpTelemetry("http://prom", "http://loki", client=client)
    now = datetime.now(UTC)
    m = await t.metrics(TENANT, "shop", window_s=600, step_s=30)
    assert m.summary.rps == 2.5 and [p.v for p in m.rps] == [2.5]
    logs = await t.logs(
        TENANT, "shop", since=now - timedelta(days=400), until=now, limit=10, status_class=2
    )
    assert [e.status for e in logs] == [200]
    loki = [
        r
        for r in seen
        if r.url.path == "/loki/api/v1/query_range" and "frontend" in r.url.params["query"]
    ]
    assert all(f'tenant="{TENANT}"' in r.url.params["query"] for r in loki)
    assert any("200".replace("200", "2") in r.url.params["query"] for r in loki)


async def test_a_failing_backend_is_a_telemetry_error() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    t = HttpTelemetry("http://prom", client=client)
    with pytest.raises(TelemetryError):
        await t.metrics(TENANT, "shop", window_s=60, step_s=15)
    with pytest.raises(TelemetryError):
        await t.logs(TENANT, "shop", since=datetime.now(UTC), until=datetime.now(UTC), limit=1)
    assert json.dumps({}) == "{}"
