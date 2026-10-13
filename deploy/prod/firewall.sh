#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Host firewall of a controller node: etcd (2379, 2380), Patroni's REST API (8008) and PostgreSQL (5432) answer only the three
# controller nodes (and, for 5432, this stack's Docker network). It adds its own nftables table and leaves your rules alone.
#   sh firewall.sh                 # read node.env in this directory, apply now
#   sh firewall.sh --print         # show the rules only
#   sh firewall.sh --persist       # also write /etc/nftables.d/switchyard.nft (included by the default nftables.conf)
# The services run on the host network, so the "input" hook is the one that matters.
set -eu
DIR=$(dirname "$0")
# shellcheck disable=SC1091
. "$DIR/node.env"
: "${NODE_IPS:?NODE_IPS is missing in node.env}"
SUBNET=${DOCKER_SUBNET:-172.29.77.0/24}
NODES=$(echo "$NODE_IPS" | tr -d ' ')
RULES=$(cat <<NFT
table inet switchyard_db {
  set nodes { type ipv4_addr; elements = { ${NODES} } }
  chain input {
    type filter hook input priority -10; policy accept;
    ct state established,related accept
    iifname "lo" accept
    tcp dport { 2379, 2380, 8008 } ip saddr @nodes accept
    tcp dport { 2379, 2380, 8008 } ip saddr ${SUBNET} accept
    tcp dport { 2379, 2380, 8008 } drop
    tcp dport 5432 ip saddr @nodes accept
    tcp dport 5432 ip saddr ${SUBNET} accept
    tcp dport 5432 drop
  }
}
NFT
)
case "${1:-}" in
  --print) echo "$RULES" ;;
  *)
    nft delete table inet switchyard_db 2>/dev/null || true
    echo "$RULES" | nft -f -
    if [ "${1:-}" = "--persist" ]; then
      mkdir -p /etc/nftables.d
      echo "$RULES" > /etc/nftables.d/switchyard.nft
      grep -q 'nftables.d' /etc/nftables.conf 2>/dev/null || echo 'include "/etc/nftables.d/*.nft"' >> /etc/nftables.conf
      systemctl enable nftables >/dev/null 2>&1 || true
    fi
    echo "switchyard_db table loaded"
    ;;
esac
