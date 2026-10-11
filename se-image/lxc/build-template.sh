#!/bin/sh
# Builds the SE *container* template on a Proxmox node (run as root on the node). Usage:
#   build-template.sh VMID AGENT_BIN DATAPLANE_BIN [STORAGE]
# Creates a privileged Debian 13 container, runs provision.sh inside it, stops it and converts it to a
# template. Clone it with VmSpec kind "lxc" (deploy request guest_kind: "lxc"; ADR 0055).
#
# The host must provide what a container cannot: load `ip_vs ip_vs_rr ip_vs_wrr ip_vs_sh nf_conntrack` on the
# Proxmox node (/etc/modules-load.d/), and keep the container privileged with nesting (set below).
set -eu
VMID=${1:?template vmid}; AGENT=${2:?path to sy-agent}; DP=${3:?path to dataplaneapi}; STORAGE=${4:-local}
HERE=$(cd "$(dirname "$0")/.." && pwd)
IMG=debian-13-standard_13.6-1_amd64.tar.zst
pveam list "$STORAGE" | grep -q "$IMG" || { pveam update >/dev/null; pveam download "$STORAGE" "$IMG"; }
pct create "$VMID" "$STORAGE:vztmpl/$IMG" --hostname sy-se-template --memory 1024 --cores 2 \
  --unprivileged 0 --features nesting=1 --net0 name=eth0,bridge=vmbr0,ip=dhcp --rootfs "$STORAGE:4"
pct start "$VMID"
n=0; until pct exec "$VMID" -- sh -c 'getent hosts deb.debian.org >/dev/null 2>&1'; do
  n=$((n+1)); [ $n -lt 30 ] || { echo "container has no network" >&2; exit 1; }; sleep 2; done
pct exec "$VMID" -- mkdir -p /opt/build/se-image/files /opt/build/se-image/test
for f in provision.sh packages.txt; do pct push "$VMID" "$HERE/$f" "/opt/build/se-image/$f" --perms 755; done
for f in "$HERE"/files/*; do pct push "$VMID" "$f" "/opt/build/se-image/files/$(basename "$f")"; done
pct push "$VMID" "$AGENT" /opt/build/sy-agent --perms 755
pct push "$VMID" "$DP" /opt/build/dataplaneapi --perms 755
pct exec "$VMID" -- sh -c 'AGENT_BIN=/opt/build/sy-agent DATAPLANE_BIN=/opt/build/dataplaneapi /opt/build/se-image/provision.sh && rm -f /opt/build/sy-agent /opt/build/dataplaneapi'
pct shutdown "$VMID" --timeout 60 || pct stop "$VMID"
pct template "$VMID"
echo "container template $VMID ready"
