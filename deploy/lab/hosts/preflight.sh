#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Read-only checks of the hosts lab from this machine, before the scenario runs. Usage: sh preflight.sh [hosts.env]
set -u
cd "$(dirname "$0")"
ENVFILE=${1:-hosts.env}
[ -f "$ENVFILE" ] || { echo "copy hosts.env.example to hosts.env and fill it in" >&2; exit 2; }
# shellcheck disable=SC1090
. "./$ENVFILE"
FAILS=0
ok()   { echo "OK    $*"; }
warn() { echo "WARN  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS+1)); }
API() { curl -sS --ssl-no-revoke ${PVE_CA_FILE:+--cacert "$PVE_CA_FILE"} -H "Authorization: PVEAPIToken=$PVE_TOKEN_ID=$PVE_TOKEN_SECRET" "$PVE_URL/api2/json$1"; }
J() { python -c "import json,sys;d=json.load(sys.stdin)['data'];$1"; }

API /version | J "print('Proxmox VE', d['version'])" >/dev/null 2>&1 \
  && ok "API login works ($(API /version | J "print(d['version'])"))" || { fail "cannot log in to $PVE_URL with that token (URL, token id, secret, CA?)"; exit 1; }
N=$(API /nodes | J "print(' '.join(n['node'] for n in d if n['status']=='online'))")
for n in $NODES; do case " $N " in *" $n "*) ok "node $n online" ;; *) fail "node $n is not online (online: $N)" ;; esac; done
[ "$(echo $NODES | wc -w)" -ge 2 ] && ok "two or more nodes listed" || warn "only one node listed: cross-host scenarios will be skipped"
API /cluster/status | J "import sys;sys.exit(0 if any(x.get('type')=='cluster' and x.get('quorate') for x in d) else 1)" \
  && ok "cluster has quorum" || warn "no cluster quorum (or not a cluster): VM placement across nodes needs one"
for v in "$VNET_MGMT" "$VNET_VIP" "$VNET_BACKEND"; do
  API /cluster/sdn/vnets | J "sys_exit=0 if any(x['vnet']=='$v' for x in d) else 1;import sys;sys.exit(sys_exit)" \
    && ok "VNet $v exists" || warn "VNet $v not found in SDN (a Linux bridge of that name is fine; check on the nodes)"
done
idx=0
for n in $NODES; do
  for o in 1 2 3; do
    t=$((TEMPLATE_BASE + 10 * idx + o))
    API "/cluster/resources?type=vm" | J "import sys;m=[x for x in d if x['vmid']==$t];print('template' if m and m[0].get('template') else ('exists-not-template' if m else 'missing'))" > /tmp/.sy.$$ 2>/dev/null
    case "$(cat /tmp/.sy.$$ 2>/dev/null)" in template) ok "template $t ($n) is a real Proxmox template" ;; exists-not-template) fail "guest $t exists but is not a template" ;; *) warn "template $t ($n) not built yet" ;; esac
  done
  idx=$((idx+1))
done
rm -f /tmp/.sy.$$
for n in $NODES; do
  eval "target=\${SSH_$n:-}"
  [ -n "$target" ] || { warn "no SSH_$n set: skipping the per-node checks for $n"; continue; }
  ssh -o BatchMode=yes -o ConnectTimeout=6 "$target" 'test -e /proc/net/ip_vs && echo ipvs=yes || echo ipvs=no; test -c /dev/kvm && echo kvm=yes || echo kvm=no' >/tmp/.sy.$$ 2>&1 \
    && { ok "ssh to $n works ($(tr '\n' ' ' < /tmp/.sy.$$))"; grep -q "ipvs=no" /tmp/.sy.$$ && warn "$n: no IPVS (UDP scenario needs it)"; grep -q "kvm=no" /tmp/.sy.$$ && warn "$n: no /dev/kvm (VM scenario needs it)"; } \
    || fail "ssh to $n ($target) failed: $(cat /tmp/.sy.$$)"
done
rm -f /tmp/.sy.$$
echo
[ "$FAILS" = 0 ] && echo "preflight passed" || { echo "$FAILS check(s) failed"; exit 1; }
