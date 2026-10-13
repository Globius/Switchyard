#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Produces the binaries the SE template needs: sy-agent built from agent/ and the Data Plane API (the version that matches
# HAProxy) copied out of the haproxytech image, both with Docker, and oauth2-proxy (the auth sidecar slot) downloaded from
# its release and checked against the pinned checksum (versions.env); coraza-spoa (the WAF slot) is built from its tag and the Core
# Rule Set tarball checked the same way. Output: se-image/dist/. WITH_AUTH=0 / WITH_WAF=0 skip the sidecars.
set -eu
cd "$(dirname "$0")/.."
. se-image/versions.env
export MSYS_NO_PATHCONV=1
ROOT=$(pwd -W 2>/dev/null || pwd)
mkdir -p se-image/dist
docker run --rm -e CGO_ENABLED=0 -v "$ROOT/agent:/src:ro" -v "$ROOT/se-image/dist:/out" -w /src "$GO_IMAGE" \
  go build -trimpath -ldflags "-X switchyard/agent/internal/status.Version=${SY_VERSION:-dev}" -o /out/sy-agent ./cmd/sy-agent
cid=$(docker create "$DATAPLANE_IMAGE")
docker cp "$cid:/usr/local/bin/dataplaneapi" se-image/dist/dataplaneapi
docker rm "$cid" >/dev/null
if [ "${WITH_AUTH:-1}" = 1 ]; then
  tgz=oauth2-proxy-$OAUTH2_PROXY_VERSION.linux-amd64.tar.gz
  curl -fsSL -o "se-image/dist/$tgz" "https://github.com/oauth2-proxy/oauth2-proxy/releases/download/$OAUTH2_PROXY_VERSION/$tgz"
  echo "$OAUTH2_PROXY_SHA256_LINUX_AMD64  se-image/dist/$tgz" | sha256sum -c - >/dev/null || { echo "oauth2-proxy checksum mismatch" >&2; exit 1; }
  tar -xzf "se-image/dist/$tgz" -C se-image/dist --strip-components=1 "oauth2-proxy-$OAUTH2_PROXY_VERSION.linux-amd64/oauth2-proxy"
  rm -f "se-image/dist/$tgz"
  (cd se-image/dist && sha256sum oauth2-proxy > oauth2-proxy.sha256)   # provision.sh checks the binary against this
fi
if [ "${WITH_WAF:-1}" = 1 ]; then   # the WAF sidecar slot: coraza-spoa built from source, and the pinned Core Rule Set
  docker run --rm -e CGO_ENABLED=0 -e GOBIN=/out -v "$ROOT/se-image/dist:/out" "$GO_IMAGE"     go install "github.com/corazawaf/coraza-spoa@$CORAZA_SPOA_VERSION"
  curl -fsSL -o se-image/dist/coreruleset.tar.gz "https://github.com/coreruleset/coreruleset/archive/refs/tags/v$CRS_VERSION.tar.gz"
  echo "$CRS_SHA256  se-image/dist/coreruleset.tar.gz" | sha256sum -c - >/dev/null || { echo "CRS checksum mismatch" >&2; exit 1; }
  (cd se-image/dist && sha256sum coraza-spoa > coraza-spoa.sha256)
fi
chmod +x se-image/dist/sy-agent se-image/dist/dataplaneapi
ls -l se-image/dist
