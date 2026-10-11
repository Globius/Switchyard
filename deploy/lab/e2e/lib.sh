#!/bin/sh
# Shared helpers for run.sh and scenario.sh.
export MSYS_NO_PATHCONV=1
PVE=switchyard-pve-lab-pve-1
TEMPLATE=9961
fail() { echo "E2E FAIL: $*" >&2; exit 1; }
step() { echo "== $*"; }
api() { cmd=$1; shift; docker exec -i -e SY_TOKEN="$(cat work/token)" -e TEMPLATE_VMID=$TEMPLATE sy-e2e-api python - "$cmd" "$@" < drive.py; }
pct() { docker exec $PVE pct "$@"; }
# Wait until "$2" succeeds, at most $3 times 3 seconds; "$1" names the condition.
wait_for() { n=0; until eval "$2" >/dev/null 2>&1; do n=$((n+1)); [ $n -lt ${3:-30} ] || fail "timed out waiting for: $1"; sleep 3; done; }
