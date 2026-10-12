#!/bin/sh
# Shared helpers for run.sh and scenario.sh of the hosts lab. Sourced after hosts.env.
fail() { echo "HOSTS LAB FAIL: $*" >&2; exit 1; }
step() { echo "== $*"; }
QS=../../quickstart
# ssh to a node by its Proxmox name (SSH_<node>=root@address in hosts.env)
# SSH_OPTS (hosts.env) adds options such as -i KEY. IPv6 addresses go in without brackets: root@2001:db8::1.
nssh() { node=$1; shift; eval "target=\$SSH_$node"; ssh ${SSH_OPTS:-} -o BatchMode=yes -o ConnectTimeout=8 "$target" "$@"; }
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
# CONTROLLER_EXEC (hosts.env) runs `api.sh` where the controller lives, for a controller in a guest, e.g.
# "ssh -i KEY root@node pct exec 9980 -- /usr/local/bin/sy-api" (install api.sh there). Default: this machine.
api() { cmd=$1; shift
  if [ -n "${CONTROLLER_EXEC:-}" ]; then $CONTROLLER_EXEC "$cmd" "$@" < ../e2e/drive.py; return; fi
  docker compose -f $QS/compose.yaml --env-file $QS/.env exec -T -e SY_TOKEN="$(cat $QS/secrets/admin-token)" \
    -e LAB_URL=http://127.0.0.1:8000 -e LAB_CA=/etc/ssl/certs/ca-certificates.crt -e CRED_REF=kv/se/dataplane api python - "$cmd" "$@" < ../e2e/drive.py; }
getj() { python -c "import json,sys;d=json.load(sys.stdin);$1"; }
# shq quotes each argument for the remote shell that ssh starts, so arguments with spaces or metacharacters arrive whole.
shq() { for a in "$@"; do printf "'%s' " "$(printf %s "$a" | sed "s/'/'\\''/g")"; done; }
# Run a command in a guest on its node. $1 kind (lxc|qemu), $2 node, $3 vmid
gexec() { kind=$1; node=$2; id=$3; shift 3
  if [ "$kind" = lxc ]; then nssh "$node" "pct exec $id -- $(shq "$@")"
  else nssh "$node" "qm guest exec $id -- $(shq "$@")" | python -c "import json,sys;d=json.load(sys.stdin);sys.stdout.write(d.get('out-data',''));sys.stderr.write(d.get('err-data',''));sys.exit(d.get('exitcode',1))"; fi; }
gctl() { kind=$1; node=$2; verb=$3; id=$4; if [ "$kind" = lxc ]; then nssh "$node" pct "$verb" "$id"; else nssh "$node" qm "$verb" "$id"; fi; }
# The helper guest (client, web and UDP backend, BGP upstream) lives on the first node.
helper() { nssh "$FIRST_NODE" "pct exec 9970 -- $(shq "$@")"; }
wait_for() { n=0; until eval "$2" >/dev/null 2>&1; do n=$((n+1)); [ $n -lt ${3:-30} ] || fail "timed out waiting for: $1"; sleep 3; done; }
vip_ok() { [ "$(helper curl -s -m 2 -o /dev/null -w '%{http_code}' "http://$1/")" = 200 ]; }

# Enroll the engines of a deploy answer (file $1) that carry a token; $2 is lxc|qemu. A controller in a guest keeps its
# secrets there: set SECRETS_DIR to a local copy (ca.pem, dp_password) and HTTPS_PORT; HOST_ADDR defaults to CONTROLLER_ADDR.
enroll_all() {
  python -c 'import json,sys
for e in json.load(open(sys.argv[1]))["engines"]:
    if "token" in e: print(e["pve_node"], e["vmid"], e["token"]["token"])' "$1" | tr -d '\r' > "$1.tokens"
  exec 3< "$1.tokens"
  while read -r node vmid token <&3; do   # not a pipe: ssh would eat the loop's input
    HOST_ADDR=${HOST_ADDR:-$CONTROLLER_ADDR} NODE_SSH="ssh ${SSH_OPTS:-} -o BatchMode=yes $(eval echo \$SSH_$node)"       sh $QS/enroll-engine.sh --kind "$2" --node "$node" --vmid "$vmid" --token "$token" >/dev/null       || echo "enrollment of $vmid on $node reported an error (the guest may still be booting)" >&2
  done; exec 3<&-; rm -f "$1.tokens"; }

# Containers do not take the bridge MTU the way VM NICs do (Proxmox's `mtu=` reaches only the host end of the veth). The
# lab's cross-host overlay is a VXLAN (MTU $LAB_MTU on the bridges), so lower the MTU on every NIC of a deployed container,
# or full-size TLS records are dropped and gRPC calls time out. Lasts until the container restarts.
# $1 node, $2 vmid. A no-op when LAB_MTU is unset (a real network carries 1500).
lxc_mtu() {
  [ -n "${LAB_MTU:-}" ] || return 0
  nssh "$1" "pct exec $2 -- sh -c 'for n in 0 1 2 3; do ip link set eth\$n mtu $LAB_MTU 2>/dev/null; done; true'"
}
lxc_mtu_all() { # deploy-output.json: every container in a deploy answer
  python -c 'import json,sys
for e in json.load(open(sys.argv[1]))["engines"]:
    if e.get("guest_kind") == "lxc": print(e["pve_node"], e["vmid"])' "$1" | tr -d '\r' > "$1.lxc"
  exec 4< "$1.lxc"
  while read -r node vmid <&4; do lxc_mtu "$node" "$vmid"; done; exec 4<&-; rm -f "$1.lxc"; }
