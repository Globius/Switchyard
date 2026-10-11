#!/bin/sh
# Produces the two binaries the SE template needs, with Docker only: builds sy-agent from agent/ and copies the Data
# Plane API (v3.2, the version that matches HAProxy 3.2) out of the haproxytech image. Output: se-image/dist/.
set -eu
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1
ROOT=$(pwd -W 2>/dev/null || pwd)
mkdir -p se-image/dist
docker run --rm -e CGO_ENABLED=0 -v "$ROOT/agent:/src:ro" -v "$ROOT/se-image/dist:/out" -w /src golang:1.26 \
  go build -trimpath -o /out/sy-agent ./cmd/sy-agent
cid=$(docker create haproxytech/haproxy-debian:3.2)
docker cp "$cid:/usr/local/bin/dataplaneapi" se-image/dist/dataplaneapi
docker rm "$cid" >/dev/null
chmod +x se-image/dist/sy-agent se-image/dist/dataplaneapi
ls -l se-image/dist
