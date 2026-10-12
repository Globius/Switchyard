#!/bin/sh
# Produces the binaries the SE template needs: sy-agent built from agent/ and the Data Plane API (the version that matches
# HAProxy) copied out of the haproxytech image, both with Docker, and oauth2-proxy (the auth sidecar slot) downloaded from
# its release and checked against the pinned checksum (versions.env). Output: se-image/dist/. WITH_AUTH=0 skips oauth2-proxy.
set -eu
cd "$(dirname "$0")/.."
. se-image/versions.env
export MSYS_NO_PATHCONV=1
ROOT=$(pwd -W 2>/dev/null || pwd)
mkdir -p se-image/dist
docker run --rm -e CGO_ENABLED=0 -v "$ROOT/agent:/src:ro" -v "$ROOT/se-image/dist:/out" -w /src "$GO_IMAGE" \
  go build -trimpath -o /out/sy-agent ./cmd/sy-agent
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
chmod +x se-image/dist/sy-agent se-image/dist/dataplaneapi
ls -l se-image/dist
