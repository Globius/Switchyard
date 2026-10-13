#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Adds a published bundle to the signed release feed that controllers read (ADR 0205, 0206).
#
#   tools/release/feed.sh BUNDLE BUNDLE_URL FEED_FILE [PREVIOUS_FEED_URL]
#
#   BUNDLE              the .sybundle file
#   BUNDLE_URL          where that file will be downloadable (https)
#   FEED_FILE           where to write the feed, for example dist/releases.json or dist/nightly.json
#   PREVIOUS_FEED_URL   the published feed to extend; a 404 means there is none yet
#
# The channel is taken from the version (stable, candidate or nightly). A nightly feed keeps the newest 14
# nightlies (FEED_KEEP overrides). Same environment as bundle.sh for the key.
set -eu
[ $# -ge 3 ] || { echo "usage: $0 BUNDLE BUNDLE_URL FEED_FILE [PREVIOUS_FEED_URL]" >&2; exit 2; }
BUNDLE=$1
URL=$2
FEED=$3
PREVIOUS_URL=${4:-}
cd "$(dirname "$0")/../.."
PY=${PYTHON:-python3}
# CI secrets are easiest to store as one line, so the key may be given base64-encoded instead.
[ -z "${RELEASE_SIGNING_KEY_B64:-}" ] || RELEASE_SIGNING_KEY=$(printf %s "$RELEASE_SIGNING_KEY_B64" | base64 -d)
: "${RELEASE_SIGNING_KEY:?RELEASE_SIGNING_KEY (or RELEASE_SIGNING_KEY_B64) is not set}"
KEY_ID=${RELEASE_KEY_ID:-release}
SYCTL=${SYCTL:-uv run --project controller --quiet syctl}

VERSION=$(basename "$BUNDLE" .sybundle)
VERSION=${VERSION#switchyard-}
CHANNEL=$($PY tools/release/version.py channel "$VERSION")

umask 077
KEYFILE=$(mktemp)
PREV=$(mktemp)
trap 'rm -f "$KEYFILE" "$PREV"' EXIT INT TERM
printf '%s\n' "$RELEASE_SIGNING_KEY" >"$KEYFILE"

set -- "$FEED" --key "$KEYFILE" --key-id "$KEY_ID" --channel "$CHANNEL" --bundle "$BUNDLE=$URL"
if [ -n "$PREVIOUS_URL" ]; then
  code=$(curl -sS -L -o "$PREV" -w '%{http_code}' "$PREVIOUS_URL" || echo 000)
  case "$code" in
    200) set -- "$@" --previous "$PREV" ;;
    404) echo "no earlier feed at $PREVIOUS_URL; starting a new one" >&2 ;;
    *) echo "could not fetch the earlier feed (HTTP $code); refusing to publish a feed that forgets older releases" >&2; exit 1 ;;
  esac
fi
[ "$CHANNEL" != nightly ] || set -- "$@" --keep "${FEED_KEEP:-14}"
$SYCTL release feed "$@"
