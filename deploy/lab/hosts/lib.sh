#!/bin/sh
# Shared helpers for run.sh and scenario.sh of the hosts lab. Sourced after hosts.env.
fail() { echo "HOSTS LAB FAIL: $*" >&2; exit 1; }
step() { echo "== $*"; }
QS=../../quickstart
# ssh to a node by its Proxmox name (SSH_<node>=root@address in hosts.env)
nssh() { node=$1; shift; eval "target=\$SSH_$node"; ssh -o BatchMode=yes -o ConnectTimeout=8 "$target" "$@"; }
node_addr() { eval "target=\$SSH_$1"; echo "${target#*@}"; }
FIRST_NODE=$(echo $NODES | awk '{print $1}')
SECOND_NODE=$(echo $NODES | awk '{print $2}')
# Template id of a kind (lxc, vm, unpriv) on a node: TEMPLATE_BASE + 10 per node + 1, 2, 3.
template_id() { # kind node
  i=0; idx=0; for n in $NODES; do [ "$n" = "$2" ] && idx=$i; i=$((i+1)); done
  case $1 in lxc) o=1 ;; vm) o=2 ;; unpriv) o=3 ;; esac
  echo $((TEMPLATE_BASE + 10 * idx + o))
}
# The controller API from inside the quickstart api container (so the TLS front is not involved).
api() { cmd=$1; shift
  docker compose -f $QS/compose.yaml --env-file $QS/.env exec -T -e SY_TOKEN="$(cat $QS/secrets/admin-token)" \
    -e LAB_URL=http://127.0.0.1:8000 -e CRED_REF=kv/se/dataplane api python - "$cmd" "$@" < ../e2e/drive.py; }
getj() { python -c "import json,sys;d=json.load(sys.stdin);$1"; }
# Run a command in a guest on its node. $1 kind (lxc|qemu), $2 node, $3 vmid
gexec() { kind=$1; node=$2; id=$3; shift 3
  if [ "$kind" = lxc ]; then nssh "$node" pct exec "$id" -- "$@"
  else nssh "$node" qm guest exec "$id" -- "$@" | python -c "import json,sys;d=json.load(sys.stdin);sys.stdout.write(d.get('out-data',''));sys.stderr.write(d.get('err-data',''));sys.exit(d.get('exitcode',1))"; fi; }
gctl() { kind=$1; node=$2; verb=$3; id=$4; if [ "$kind" = lxc ]; then nssh "$node" pct "$verb" "$id"; else nssh "$node" qm "$verb" "$id"; fi; }
# The helper guest (client, web and UDP backend, BGP upstream) lives on the first node.
helper() { nssh "$FIRST_NODE" pct exec 9970 -- "$@"; }
wait_for() { n=0; until eval "$2" >/dev/null 2>&1; do n=$((n+1)); [ $n -lt ${3:-30} ] || fail "timed out waiting for: $1"; sleep 3; done; }
vip_ok() { [ "$(helper curl -s -m 2 -o /dev/null -w '%{http_code}' "http://$1/")" = 200 ]; }
