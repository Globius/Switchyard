#!/bin/sh
# Hands a new engine its enrollment material (what cloud-init will do once snippet delivery exists, ADR 0011): the
# controller address, the one-time token, the controller CA and the Data Plane credentials, then starts enrollment.
#   sh enroll-engine.sh --kind lxc|qemu --node NODE --vmid ID --token TOKEN [--vector-env FILE]
#   sh enroll-engine.sh --kind lxc|qemu --node NODE --vmid ID --vector-env FILE        (log shipping only, no token)
# --vector-env FILE is the engine's /etc/switchyard/vector.env (LOKI_URL, TENANT_ID, TENANT_NAME, SE_GROUP, ENGINE): the
# cloud-init path writes it, and without it Vector does not run, so no access logs reach Loki (runbooks/telemetry.md).
# Runs `pct`/`qm` on the Proxmox node through ssh; set NODE_SSH to override (default "ssh root@NODE", an IPv6 node
# address needs no brackets). When the controller runs on another machine, point SECRETS_DIR at a copy of its `secrets/`
# (ca.pem, dp_password) and set HOST_ADDR (and HTTPS_PORT) to the address the engines enroll to.
# The secrets travel through stdin into files of mode 600 inside the guest (`pct exec`, or the QEMU guest agent for
# VMs: the template has qemu-guest-agent); nothing is kept on the node. The VM path has not run on real hardware yet.
set -eu
cd "$(dirname "$0")"
KIND=""; NODE=""; VMID=""; TOKEN=""; VECTOR_ENV=""
while [ $# -gt 0 ]; do case "$1" in
  --kind) KIND=$2; shift 2 ;; --node) NODE=$2; shift 2 ;; --vmid) VMID=$2; shift 2 ;; --token) TOKEN=$2; shift 2 ;; --vector-env) VECTOR_ENV=$2; shift 2 ;;
  *) echo "see the header of this script" >&2; exit 2 ;; esac; done
[ -n "$KIND" ] && [ -n "$NODE" ] && [ -n "$VMID" ] && { [ -n "$TOKEN" ] || [ -n "$VECTOR_ENV" ]; } || { echo "--kind, --node, --vmid and --token (or --vector-env) are required" >&2; exit 2; }
SECRETS_DIR=${SECRETS_DIR:-secrets}
ENVGET() { sed -n "s/^$1=//p" .env 2>/dev/null | head -1; }
HOST=${HOST_ADDR:-$(ENVGET HOST_ADDR)}; PORT=${HTTPS_PORT:-$(ENVGET HTTPS_PORT)}; PORT=${PORT:-8443}
case "$HOST" in *:*) HOST="[$HOST]" ;; esac
NODE_SSH=${NODE_SSH:-ssh root@$NODE}
DEST=/etc/switchyard/agent
put() { # path mode   (content on stdin)
  case "$KIND" in
    lxc) $NODE_SSH "pct exec $VMID -- sh -c 'umask 077; cat > $1; chmod $2 $1'" ;;
    qemu) $NODE_SSH "qm guest exec $VMID --pass-stdin 1 -- sh -c 'umask 077; cat > $1; chmod $2 $1'" >/dev/null ;;
    *) echo "--kind must be lxc or qemu" >&2; exit 2 ;;
  esac
}
svc() { # systemctl verb unit, in the guest
  case "$KIND" in
    lxc) $NODE_SSH pct exec "$VMID" -- systemctl "$@" ;;
    qemu) $NODE_SSH qm guest exec "$VMID" -- systemctl "$@" >/dev/null ;;
  esac
}
if [ -n "$VECTOR_ENV" ]; then
  put /etc/switchyard/vector.env 644 < "$VECTOR_ENV"
  svc restart vector
  echo "log shipping configured on $KIND $VMID"
fi
[ -n "$TOKEN" ] || exit 0
printf 'CONTROLLER_URL=https://%s:%s\nCONTROLLER_CN=switchyard-controller\nDP_USER=switchyard\nDP_PASSWORD=%s\n' \
  "$HOST" "$PORT" "$(cat "$SECRETS_DIR/dp_password")" | put $DEST/enroll.env 600
printf '%s\n' "$TOKEN" | put $DEST/enroll.token 600
put $DEST/controller-ca.crt 644 < "$SECRETS_DIR/ca.pem"
svc start sy-agent-enroll.service
echo "enrollment started on $KIND $VMID"
