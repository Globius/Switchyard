#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Builds the proxy, runs it in front of the controller image and checks headers, routing and rate limiting.
# Needs the dev PostgreSQL and: docker build -f deploy/controller/Dockerfile -t switchyard-controller:dev .
set -eu
cd "$(dirname "$0")/../.."
docker build -q -f deploy/proxy/Dockerfile -t switchyard-proxy . >/dev/null
docker network create sy-ptest >/dev/null 2>&1 || true
trap 'docker rm -f sy-ptest-api sy-ptest-proxy >/dev/null 2>&1; docker network rm sy-ptest >/dev/null 2>&1' EXIT
docker run -d --name sy-ptest-api --network sy-ptest --network-alias api \
  -e SWITCHYARD_DATABASE_URL=postgresql+psycopg://postgres:postgres@host.docker.internal:5432/switchyard \
  switchyard-controller:dev >/dev/null
docker run -d --name sy-ptest-proxy --network sy-ptest -p 18080:8080 switchyard-proxy >/dev/null
n=0; until curl -sf localhost:18080/api/v1/healthz >/dev/null; do n=$((n+1)); [ $n -lt 30 ] || { echo "FAIL: no health"; exit 1; }; sleep 2; done
fail() { echo "FAIL: $*"; exit 1; }
H=$(curl -si localhost:18080/)
for h in "Content-Security-Policy: default-src 'self'" "X-Content-Type-Options: nosniff" "Referrer-Policy: same-origin" "frame-ancestors 'none'"; do
  echo "$H" | grep -qF "$h" || fail "missing header $h"
done
[ "$(curl -s -o /dev/null -w %{http_code} localhost:18080/virtual-services)" = 200 ] || fail "SPA fallback"
[ "$(curl -s --http1.0 -H 'Host:' -o /dev/null -w %{http_code} localhost:18080/api/v1/healthz)" = 200 ] || fail "HTTP/1.0 health check without a Host header"
curl -si -H 'X-Forwarded-Proto: https' localhost:18080/api/v1/healthz | grep -qi "Strict-Transport-Security: max-age=31536000" || fail "HSTS over https"
codes=$(for i in $(seq 1 16); do curl -s -o /dev/null -w "%{http_code} " -X POST localhost:18080/api/v1/auth/break-glass -H 'content-type: application/json' -d '{}' || echo "curl-failed($?) "; done)
echo "$codes" | grep -q 429 || fail "login not rate limited: $codes"
echo "proxy smoke passed"
