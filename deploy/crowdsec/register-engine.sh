#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# One CrowdSec bouncer key per Switchyard engine (ADR 0215). CrowdSec's local API cannot register a bouncer remotely, so an
# operator does it here, when an engine joins or its key is rotated:
#   ./register-engine.sh add edge-1      # new key: registered in CrowdSec, stored in OpenBao, never printed
#   ./register-engine.sh revoke edge-1   # delete the bouncer in CrowdSec and the key in OpenBao (engine retired, key leaked)
# To rotate: revoke, add, then re-apply that engine's SE group. The other engines are not touched.
# Needs Admin > Integrations > "One key per engine" on. Run in deploy/crowdsec/ with BAO_ADDR and a token that may write kv/system/crowdsec/.
# Overrides for tests and other setups: CSCLI (default: docker compose exec -T crowdsec cscli), BAO (default: bao).
set -eu
usage() { echo "usage: $0 add|revoke <engine-name>" >&2; exit 2; }
[ $# -eq 2 ] || usage
action=$1
engine=$2
case "$engine" in
  ''|*[!a-z0-9-]*|-*|*-) echo "engine names are lower-case letters, digits and hyphens: $engine" >&2; exit 2 ;;
esac
CSCLI=${CSCLI:-docker compose exec -T crowdsec cscli}
BAO=${BAO:-bao}
ref="kv/system/crowdsec/bouncer-keys/$engine"
case "$action" in
  add)
    key=$(openssl rand -hex 32)
    # shellcheck disable=SC2086  # CSCLI is a command line on purpose
    $CSCLI bouncers add "$engine" -k "$key" >/dev/null
    # the key goes to OpenBao on standard input, never on a command line
    printf '%s' "$key" | $BAO kv put "$ref" value=- >/dev/null
    echo "registered bouncer $engine; key stored at $ref"
    ;;
  revoke)
    # shellcheck disable=SC2086
    $CSCLI bouncers delete "$engine"
    $BAO kv metadata delete "$ref" >/dev/null
    echo "revoked bouncer $engine; removed $ref"
    ;;
  *) usage ;;
esac
