#!/bin/sh
# Hands a new engine its enrollment material (what cloud-init will do once snippet delivery exists, ADR 0011): the
# controller address, the one-time token, the controller CA and the Data Plane credentials, then starts enrollment.
#   sh enroll-engine.sh --kind lxc|qemu --node NODE --vmid ID --token TOKEN
# Runs `pct`/`pvesh` on the Proxmox node through ssh; set NODE_SSH to override (default "ssh root@NODE").
# The secrets travel through stdin into files of mode 600 inside the guest (`pct exec`, or the QEMU guest agent for
# VMs: the template has qemu-guest-agent); nothing is kept on the node. The VM path has not run on real hardware yet.
set -eu
cd "$(dirname "$0")"
KIND=""; NODE=""; VMID=""; TOKEN=""
while [ $# -gt 0 ]; do case "$1" in
  --kind) KIND=$2; shift 2 ;; --node) NODE=$2; shift 2 ;; --vmid) VMID=$2; shift 2 ;; --token) TOKEN=$2; shift 2 ;;
  *) echo "see the header of this script" >&2; exit 2 ;; esac; done
[ -n "$KIND" ] && [ -n "$NODE" ] && [ -n "$VMID" ] && [ -n "$TOKEN" ] || { echo "--kind, --node, --vmid, --token are required" >&2; exit 2; }
ENVGET() { sed -n "s/^$1=//p" .env | head -1; }
HOST=$(ENVGET HOST_ADDR); PORT=$(ENVGET HTTPS_PORT); PORT=${PORT:-8443}
NODE_SSH=${NODE_SSH:-ssh root@$NODE}
DEST=/etc/switchyard/agent
put() { # path mode   (content on stdin)
  case "$KIND" in
    lxc) $NODE_SSH pct exec "$VMID" -- sh -c "umask 077; cat > $1; chmod $2 $1" ;;
    qemu) $NODE_SSH qm guest exec "$VMID" --pass-stdin 1 -- sh -c "umask 077; cat > $1; chmod $2 $1" >/dev/null ;;
    *) echo "--kind must be lxc or qemu" >&2; exit 2 ;;
  esac
}
printf 'CONTROLLER_URL=https://%s:%s\nCONTROLLER_CN=switchyard-controller\nDP_USER=switchyard\nDP_PASSWORD=%s\n' \
  "$HOST" "$PORT" "$(cat secrets/dp_password)" | put $DEST/enroll.env 600
printf '%s\n' "$TOKEN" | put $DEST/enroll.token 600
put $DEST/controller-ca.crt 644 < secrets/ca.pem
case "$KIND" in
  lxc) $NODE_SSH pct exec "$VMID" -- systemctl start sy-agent-enroll.service ;;
  qemu) $NODE_SSH qm guest exec "$VMID" -- systemctl start sy-agent-enroll.service ;;
esac
echo "enrollment started on $KIND $VMID"
