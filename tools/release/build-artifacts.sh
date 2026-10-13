#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Builds the release artifacts for one version into a directory (ADR 0206): the controller and proxy images
# (docker save, gzipped), the Helm chart package, the sy-agent binary and the production Compose file.
# The version is stamped into the working copy first, so run it on a throwaway checkout (CI does).
# Needs docker, helm, go and python3.
#
#   tools/release/build-artifacts.sh 0.2.0-nightly.20261014 dist
set -eu
[ $# -ge 1 ] || { echo "usage: $0 VERSION [OUTDIR]" >&2; exit 2; }
VERSION=$1
OUT=${2:-dist}
cd "$(dirname "$0")/../.."
PY=${PYTHON:-python3}
$PY tools/release/version.py channel "$VERSION" >/dev/null
$PY tools/release/version.py stamp "$VERSION"
mkdir -p "$OUT"

docker build -q -f deploy/controller/Dockerfile -t "switchyard-controller:$VERSION" .
docker build -q -f deploy/proxy/Dockerfile -t "switchyard-proxy:$VERSION" .
docker save "switchyard-controller:$VERSION" | gzip -n >"$OUT/controller.tar.gz"
docker save "switchyard-proxy:$VERSION" | gzip -n >"$OUT/proxy.tar.gz"

helm package deploy/helm/switchyard --version "$VERSION" --app-version "$VERSION" -d "$OUT" >/dev/null
mv "$OUT/switchyard-$VERSION.tgz" "$OUT/switchyard-chart.tgz"

(cd agent && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath \
  -ldflags "-s -w -X switchyard/agent/internal/status.Version=$VERSION" -o "../$OUT/sy-agent" ./cmd/sy-agent)

cp deploy/prod/compose.yaml "$OUT/compose.yaml"
ls -l "$OUT"
