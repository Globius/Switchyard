#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Packs the artifacts of one version into a signed upgrade bundle (ADR 0205, 0206).
#
#   RELEASE_SIGNING_KEY   the Ed25519 private key (PEM text), from a CI secret, or RELEASE_SIGNING_KEY_B64 (the same,
#                         base64 on one line). Never printed, written to a
#                         0600 temporary file that is removed on exit.
#   RELEASE_KEY_ID        the key's id (default: release)
#   UPGRADE_FROM          versions this one can be installed over, space separated (empty: from any)
#   NOTES_FILE            JSON with new, deprecated, breaking, action_required
#                         (default: docs/release-notes/VERSION.json when it exists)
#   SYCTL                 how to run syctl (default: uv run --project controller --quiet syctl)
#
#   tools/release/bundle.sh 0.2.0 dist     -> dist/switchyard-0.2.0.sybundle
set -eu
[ $# -ge 1 ] || { echo "usage: $0 VERSION [DIR]" >&2; exit 2; }
VERSION=$1
DIR=${2:-dist}
cd "$(dirname "$0")/../.."
# CI secrets are easiest to store as one line, so the key may be given base64-encoded instead.
[ -z "${RELEASE_SIGNING_KEY_B64:-}" ] || RELEASE_SIGNING_KEY=$(printf %s "$RELEASE_SIGNING_KEY_B64" | base64 -d)
: "${RELEASE_SIGNING_KEY:?RELEASE_SIGNING_KEY (or RELEASE_SIGNING_KEY_B64) is not set}"
KEY_ID=${RELEASE_KEY_ID:-release}
SYCTL=${SYCTL:-uv run --project controller --quiet syctl}

umask 077
KEYFILE=$(mktemp)
trap 'rm -f "$KEYFILE"' EXIT INT TERM
printf '%s\n' "$RELEASE_SIGNING_KEY" >"$KEYFILE"

NOTES=${NOTES_FILE:-}
[ -n "$NOTES" ] || { [ -f "docs/release-notes/$VERSION.json" ] && NOTES="docs/release-notes/$VERSION.json" || true; }
set -- "$DIR/switchyard-$VERSION.sybundle" --key "$KEYFILE" --key-id "$KEY_ID" --version "$VERSION" \
  --controller-image "$DIR/controller.tar.gz" --proxy-image "$DIR/proxy.tar.gz" \
  --helm-chart "$DIR/switchyard-chart.tgz" --agent "$DIR/sy-agent" --compose "$DIR/compose.yaml"
[ -z "$NOTES" ] || set -- "$@" --notes "$NOTES"
for v in ${UPGRADE_FROM:-}; do set -- "$@" --upgrade-from "$v"; done
# shellcheck disable=SC2086
$SYCTL release build "$@"
