#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# The two-host lab, start to finish (docs/runbooks/lab-hosts.md). Usage: sh deploy/lab/hosts/run.sh [hosts.env]
# Needs: the hosts prepared (prep-host.sh), hosts.env filled in, Docker and ssh on this machine. Not run on real
# hardware yet: expect to adjust the first time. Re-running is safe up to the scenario; teardown is manual (the runbook).
set -eu
cd "$(dirname "$0")"
ENVFILE=${1:-hosts.env}
[ -f "$ENVFILE" ] || { echo "copy hosts.env.example to $ENVFILE and fill it in" >&2; exit 2; }
# shellcheck disable=SC1090
. "./$ENVFILE"
. ./lib.sh
export MSYS_NO_PATHCONV=1

step "preflight"
sh preflight.sh "$ENVFILE" || fail "preflight found problems (see FAIL lines)"

step "binaries for the engine templates"
sh ../../../se-image/fetch-binaries.sh >/dev/null

step "controller on this machine ($CONTROLLER_ADDR)"
(cd $QS && sh bootstrap.sh --host "$CONTROLLER_ADDR" >/dev/null)
(cd $QS && sh connect-proxmox.sh --url "$PVE_URL" --token-id "$PVE_TOKEN_ID" --secret "$PVE_TOKEN_SECRET" --ca "$(cd "$(dirname "$PVE_CA_FILE")" && pwd)/$(basename "$PVE_CA_FILE")")

step "engine templates on every node (container, unprivileged container, VM)"
for node in $NODES; do
  ADDR=$(node_addr "$node")
  nssh "$node" mkdir -p /root/build
  scp -q -r -o BatchMode=yes ../../../se-image "$(eval echo \$SSH_$node):/root/build/"
  for kind in lxc unpriv vm; do
    id=$(template_id $kind "$node")
    if nssh "$node" "qm config $id 2>/dev/null | grep -q '^template: 1' || pct config $id 2>/dev/null | grep -q '^template: 1'"; then
      echo "   template $id ($kind, $node) exists"; continue; fi
    case $kind in
      lxc) nssh "$node" "sh /root/build/se-image/lxc/build-template.sh $id /root/build/se-image/dist/sy-agent /root/build/se-image/dist/dataplaneapi" ;;
      unpriv) nssh "$node" "UNPRIVILEGED=1 sh /root/build/se-image/lxc/build-template.sh $id /root/build/se-image/dist/sy-agent /root/build/se-image/dist/dataplaneapi" ;;
      vm) nssh "$node" "NODE_URL=http://$ADDR sh /root/build/se-image/kvm/build-template.sh $id /root/build/se-image/dist/sy-agent /root/build/se-image/dist/dataplaneapi $VM_STORAGE" ;;
    esac
  done
done

step "tenant and helper guest"
(cd $QS && sh adopt.sh ../lab/hosts/tenant.yaml)
nssh "$FIRST_NODE" "TEMPLATE=$(template_id lxc "$FIRST_NODE") VNET_MGMT=$VNET_MGMT VNET_VIP=$VNET_VIP VNET_BACKEND=$VNET_BACKEND sh -s" < helper.sh

sh scenario.sh
