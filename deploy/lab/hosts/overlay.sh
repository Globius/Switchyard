#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Builds the lab's isolated networks as VXLAN bridges between two Proxmox nodes whose own network is IPv6-only (Proxmox
# SDN cannot make a VXLAN with an IPv6 underlay, so this writes plain ifupdown2 stanzas). Run as root ON each node:
#   sh overlay.sh --self <this node's underlay address> --peer <the other node's> [--nets "labmgmt:101 labvip:102 labbe:103"]
#                 [--addr "labmgmt=172.30.0.1/24"]      # give the bridge an address on THIS node (the gateway / jump host)
#   sh overlay.sh --remove
# Each net becomes a bridge of that name (use it as the VNet / bridge name) with a VXLAN port to the peer. The MTU is 1430
# (1500 minus IPv6, UDP, VXLAN and Ethernet headers): guests left at 1500 lose frames above 1430 between the nodes.
set -eu
NETS="labmgmt:101 labvip:102 labbe:103"; SELF=""; PEER=""; REMOVE=0; ADDRS=""
while [ $# -gt 0 ]; do case "$1" in
  --self) SELF=$2; shift 2 ;; --peer) PEER=$2; shift 2 ;; --nets) NETS=$2; shift 2 ;; --addr) ADDRS="$ADDRS $2"; shift 2 ;; --remove) REMOVE=1; shift ;;
  *) echo "see the header of this script" >&2; exit 2 ;; esac; done
FILE=/etc/network/interfaces.d/switchyard-lab
if [ "$REMOVE" = 1 ]; then
  for pair in $NETS; do ifdown "${pair%%:*}" 2>/dev/null || true; done
  rm -f "$FILE"; ifreload -a; echo "overlay removed"; exit 0
fi
[ -n "$SELF" ] && [ -n "$PEER" ] || { echo "--self and --peer are required" >&2; exit 2; }
: > "$FILE"
for pair in $NETS; do
  name=${pair%%:*}; vni=${pair##*:}
  cat >> "$FILE" <<STANZA
auto $name
iface $name inet manual
	bridge_ports none
	bridge_stp off
	bridge_fd 0
	mtu 1430
	pre-up ip link add vx_$name type vxlan id $vni local $SELF remote $PEER dstport 4789 || true
	post-up ip link set vx_$name master $name mtu 1430 up
	post-down ip link del vx_$name || true
$(for a in $ADDRS; do [ "${a%%=*}" = "$name" ] && printf '	post-up ip addr add %s dev %s
' "${a#*=}" "$name"; done)

STANZA
done
ifreload -a
for pair in $NETS; do name=${pair%%:*}; ip link show "vx_$name" >/dev/null && ip link show "$name" >/dev/null && echo "OK    $name up (VNI ${pair##*:}) to $PEER"; done
