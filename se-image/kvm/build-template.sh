#!/bin/sh
# Builds the SE *VM* template on a Proxmox node (run as root on the node). Usage:
#   build-template.sh VMID AGENT_BIN DATAPLANE_BIN [STORAGE] [BRIDGE]
# Imports the Debian 13 cloud image, boots it once with cloud-init user-data that downloads se-image/, sy-agent and
# dataplaneapi from a throwaway web server on this node, runs provision.sh and powers off; then converts the VM to a
# template (clone it with deploy `guest_kind: "qemu"`). Needs /dev/kvm to be quick, and DHCP on BRIDGE.
# Environment: NODE_URL=http://<address of this node the build VM can reach> (required; an IPv6 address in brackets),
# PORT=8099, BIND=:: to serve on IPv6, BUILD_IPCONFIG="ip6=auto" for an IPv6-only network (default ip=dhcp).
set -eu
VMID=${1:?template vmid}; AGENT=${2:?path to sy-agent}; DP=${3:?path to dataplaneapi}
STORAGE=${4:-local}; BRIDGE=${5:-vmbr0}
: "${NODE_URL:?set NODE_URL to an address of this node the build VM can reach, e.g. http://172.30.0.1}"
PORT=${PORT:-8099}
HERE=$(cd "$(dirname "$0")/.." && pwd)
IMG=/var/lib/vz/template/iso/debian-13-genericcloud-amd64.qcow2
[ -s "$IMG" ] || curl -fsSL -o "$IMG" https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2

WEB=$(mktemp -d)
mkdir -p "$WEB/se-image"
cp -r "$HERE/files" "$HERE/cloud-init" "$HERE/provision.sh" "$HERE/packages.txt" "$WEB/se-image/"
cp "$AGENT" "$WEB/sy-agent"; cp "$DP" "$WEB/dataplaneapi"
tar -C "$WEB" -czf "$WEB/build.tar.gz" se-image sy-agent dataplaneapi
(cd "$WEB" && python3 -m http.server "$PORT" --bind "${BIND:-0.0.0.0}" >/dev/null 2>&1) &
WEBPID=$!
trap 'kill $WEBPID 2>/dev/null' EXIT

cat > /var/lib/vz/snippets/sy-se-template-build.yaml <<YAML
#cloud-config
package_update: true
packages: [curl, ca-certificates]
runcmd:
  - [sh, -c, "curl -g -fsS $NODE_URL:$PORT/build.tar.gz | tar -xz -C /root && AGENT_BIN=/root/sy-agent DATAPLANE_BIN=/root/dataplaneapi sh /root/se-image/provision.sh > /var/log/sy-provision.log 2>&1 && poweroff || echo BUILD FAILED > /dev/ttyS0"]
YAML

qm create "$VMID" --name sy-se-template --memory 1536 --cores 2 --cpu host --ostype l26 --agent 1 \
  --net0 "virtio,bridge=$BRIDGE" --scsihw virtio-scsi-single --serial0 socket --vga serial0
qm importdisk "$VMID" "$IMG" "$STORAGE" >/dev/null
DISK=$(qm config "$VMID" | sed -n 's/^unused0: *\([^,]*\).*/\1/p')
qm set "$VMID" --scsi0 "$DISK,discard=on" --boot order=scsi0 --ide2 "$STORAGE:cloudinit" \
  --cicustom "user=local:snippets/sy-se-template-build.yaml" --ipconfig0 "${BUILD_IPCONFIG:-ip=dhcp}" >/dev/null
qm resize "$VMID" scsi0 8G >/dev/null
qm start "$VMID"
echo "building (the VM powers itself off when provisioning is done)..."
n=0; until [ "$(qm status "$VMID" | awk '{print $2}')" = stopped ]; do
  n=$((n+1)); [ $n -lt 150 ] || { echo "build VM still running after 25 minutes" >&2; exit 1; }; sleep 10; done
qm set "$VMID" --delete cicustom >/dev/null
qm template "$VMID"
echo "VM template $VMID ready"
