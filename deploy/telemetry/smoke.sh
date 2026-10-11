#!/bin/sh
# Smoke test: real controller image (with /metrics and discovery) + the telemetry stack.
# Needs the dev PostgreSQL (deploy/dev) and a built controller image:
#   docker build -f deploy/controller/Dockerfile -t switchyard-controller:dev .
set -eu
cd "$(dirname "$0")"
TOKEN=smoke-token-$$
mkdir -p secrets && printf '%s' "$TOKEN" > secrets/metrics.token
DB=postgresql+psycopg://postgres:postgres@host.docker.internal:5432/switchyard
docker rm -f sy-telemetry-ctl >/dev/null 2>&1 || true
docker run --rm -e SWITCHYARD_DATABASE_URL="$DB" switchyard-controller:dev db upgrade >/dev/null
docker run -d --name sy-telemetry-ctl -p 18010:8000 -e SWITCHYARD_DATABASE_URL="$DB" \
  -e SWITCHYARD_METRICS_TOKEN="$TOKEN" switchyard-controller:dev >/dev/null
cleanup() { docker rm -f sy-telemetry-ctl >/dev/null 2>&1; CONTROLLER_HOSTPORT=x GRAFANA_ADMIN_PASSWORD=x docker compose down -v >/dev/null 2>&1; rm -rf secrets; }
trap cleanup EXIT
export CONTROLLER_SCHEME=http CONTROLLER_HOSTPORT=host.docker.internal:18010 GRAFANA_ADMIN_PASSWORD=smoke
docker compose up -d >/dev/null
wait_for() { n=0; until eval "$2" >/dev/null 2>&1; do n=$((n+1)); [ $n -lt 40 ] || { echo "FAIL: $1"; docker compose logs --tail 20; exit 1; }; sleep 3; done; echo "ok: $1"; }
wait_for "controller /metrics with token" "curl -sf -H 'Authorization: Bearer $TOKEN' localhost:18010/metrics | grep -q switchyard_jobs"
wait_for "controller /metrics rejects no token" "[ \"\$(curl -s -o /dev/null -w %{http_code} localhost:18010/metrics)\" != 200 ]"
wait_for "discovery answers (empty list is fine)" "curl -sf -H 'Authorization: Bearer $TOKEN' 'localhost:18010/api/v1/discovery/prometheus?exporter=node'"
wait_for "prometheus config valid" "docker compose exec -T prometheus promtool check config /tmp/prometheus.yml"
wait_for "prometheus scrapes the controller" "curl -sf 'localhost:9090/api/v1/query?query=up%7Bjob%3D%22switchyard%22%7D' | grep -q '\"value\":\[[0-9.]*,\"1\"\]'"
wait_for "loki ready" "curl -sf localhost:3100/ready"
wait_for "grafana has 4 dashboards" "[ \"\$(curl -sf -u admin:smoke 'localhost:3000/api/search?tag=switchyard' | grep -o '\"uid\"' | wc -l)\" = 4 ]"
wait_for "grafana prometheus datasource healthy" "curl -sf -u admin:smoke localhost:3000/api/datasources/uid/prometheus/health | grep -q OK"
wait_for "grafana loki datasource healthy" "curl -sf -u admin:smoke localhost:3000/api/datasources/uid/loki/health | grep -q OK"
# The controller's own client reads back what a SE's Vector would push (HttpTelemetry against real Loki).
T=0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b
NOW=$(date +%s)000000000
LINE='192.0.2.7:51514 [10/Oct/2026:08:30:00.123] vs_shop pool_web/web-1 0/0/1/12/13 200 512 - - ---- 1/1/0/0/0 0/0 \"GET /a HTTP/1.1\"'
curl -sS --fail-with-body -H 'Content-Type: application/json' localhost:3100/loki/api/v1/push   -d "{\"streams\":[{\"stream\":{\"tenant\":\"$T\",\"frontend\":\"vs_shop\"},\"values\":[[\"$NOW\",\"$LINE\"]]}]}"
wait_for "controller client reads the pushed access log" "docker run --rm --entrypoint python switchyard-controller:dev -c \"
import asyncio, uuid
from datetime import UTC, datetime, timedelta
from switchyard.adapters.telemetry import HttpTelemetry
async def go():
    t = HttpTelemetry('http://host.docker.internal:9090', 'http://host.docker.internal:3100')
    now = datetime.now(UTC)
    rows = await t.logs(uuid.UUID('$T'), 'shop', since=now - timedelta(minutes=5), until=now + timedelta(minutes=1), limit=10)
    assert rows and rows[0].status == 200 and rows[0].path == '/a', rows
    other = await t.logs(uuid.uuid4(), 'shop', since=now - timedelta(minutes=5), until=now + timedelta(minutes=1), limit=10)
    assert not other, 'another tenant must see nothing'
asyncio.run(go())
\""
echo "telemetry smoke passed"
