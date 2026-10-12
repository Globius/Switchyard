#!/bin/sh
# Shared helpers for run.sh and scenario.sh.
export MSYS_NO_PATHCONV=1
PVE=switchyard-pve-lab-pve-1
# GUEST_KIND=lxc (default) or qemu: what the engines are. The same scenario runs for both.
GUEST_KIND=${GUEST_KIND:-lxc}
case $GUEST_KIND in lxc) TEMPLATE=9961 ;; qemu) TEMPLATE=9962 ;; *) echo "GUEST_KIND must be lxc or qemu" >&2; exit 2 ;; esac
fail() { echo "E2E FAIL: $*" >&2; exit 1; }
step() { echo "== $*"; }
api() { cmd=$1; shift; docker exec -i -e SY_TOKEN="$(cat work/token)" -e TEMPLATE_VMID=$TEMPLATE -e GUEST_KIND=$GUEST_KIND sy-e2e-api python - "$cmd" "$@" < drive.py; }
pct() { docker exec $PVE pct "$@"; }
qm() { docker exec $PVE qm "$@"; }
# Run a command inside an engine (container or VM through the guest agent), with its exit code and output.
gexec() {
  id=$1; shift
  if [ "$GUEST_KIND" = lxc ]; then docker exec $PVE pct exec "$id" -- "$@"
  else docker exec $PVE qm guest exec "$id" -- "$@" | python -c "import json,sys;d=json.load(sys.stdin);sys.stdout.write(d.get('out-data',''));sys.stderr.write(d.get('err-data',''));sys.exit(d.get('exitcode',1))"
  fi
}
gstop() { if [ "$GUEST_KIND" = lxc ]; then pct stop "$1"; else qm stop "$1"; fi; }
gstart() { if [ "$GUEST_KIND" = lxc ]; then pct start "$1"; else qm start "$1"; fi; }
greboot() { if [ "$GUEST_KIND" = lxc ]; then pct reboot "$1"; else qm reboot "$1"; fi; }
# Address of extra NIC number $2 (1 or 2) as configured on the guest, from its Proxmox config.
nic_ip() {
  if [ "$GUEST_KIND" = lxc ]; then pct config "$1" | grep "^net$2:" | sed 's/.*ip=\([^,]*\).*/\1/'
  else qm config "$1" | grep "^ipconfig$2:" | sed 's/.*ip=\([^,]*\).*/\1/'; fi
}
# Wait until "$2" succeeds, at most $3 times 3 seconds; "$1" names the condition.
wait_for() { n=0; until eval "$2" >/dev/null 2>&1; do n=$((n+1)); [ $n -lt ${3:-30} ] || fail "timed out waiting for: $1"; sleep 3; done; }
