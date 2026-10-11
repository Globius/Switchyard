#!/bin/sh
# Prepares a Proxmox VE node for the Switchyard lab. Run as root ON the node (once per node):
#   sh prep-host.sh [--bridge vmbr0] [--sdn "labmgmt:101 labvip:102 labbe:103"] [--token]
# Checks and fixes what it can, prints OK / WARN / FAIL lines, and exits 1 on any FAIL.
#   --bridge B   the VLAN-aware bridge the lab VLANs ride on (default vmbr0)
#   --sdn LIST   create (cluster-wide, run on ONE node) an SDN VLAN zone `lab` with these VNet:VLAN pairs
#   --token      create the controller API user and token (run on ONE node); the secret is printed once
set -u
BRIDGE=vmbr0; SDN=""; TOKEN=0; FAILS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --bridge) BRIDGE=$2; shift 2 ;;
    --sdn) SDN=$2; shift 2 ;;
    --token) TOKEN=1; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
ok()   { echo "OK    $*"; }
warn() { echo "WARN  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS+1)); }

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
command -v pveversion >/dev/null 2>&1 || { echo "this is not a Proxmox VE node" >&2; exit 2; }
V=$(pveversion | sed 's#pve-manager/\([0-9]*\)\..*#\1#')
[ "${V:-0}" -ge 9 ] 2>/dev/null && ok "Proxmox VE $(pveversion | cut -d/ -f2 | cut -d/ -f1)" || fail "Proxmox VE 9 or newer is required ($(pveversion))"

# Hardware virtualization: needed for the KVM scenarios; containers work without it.
if [ -c /dev/kvm ]; then ok "/dev/kvm present (KVM VMs will run at full speed)"
else warn "/dev/kvm missing: enable VT-x/AMD-V in the BIOS; KVM VM scenarios will not run"; fi

# IPVS and conntrack for UDP services (containers use the host kernel's modules).
MODS="ip_vs ip_vs_rr ip_vs_wrr ip_vs_sh nf_conntrack"
for m in $MODS; do modprobe "$m" 2>/dev/null; done
if [ -e /proc/net/ip_vs ]; then
  printf '%s\n' $MODS > /etc/modules-load.d/switchyard.conf && ok "IPVS loaded and set to load at boot"
else warn "IPVS modules could not be loaded: UDP services cannot run on this node"; fi

# The VLAN-aware bridge the three lab VLANs ride on.
if ip link show "$BRIDGE" >/dev/null 2>&1; then
  if ip -d link show "$BRIDGE" | grep -q 'vlan_filtering 1'; then ok "$BRIDGE is VLAN-aware"
  else fail "$BRIDGE is not VLAN-aware: tick 'VLAN aware' on the bridge (Network tab) and apply"; fi
else fail "bridge $BRIDGE does not exist (pass --bridge)"; fi

# Storage content types the lab needs on `local`.
pvesm set local --content iso,vztmpl,backup,snippets,images,rootdir >/dev/null 2>&1 \
  && ok "storage 'local' holds snippets, container templates and images" \
  || warn "could not set content types on storage 'local'"

# Container template (privileged and unprivileged engines are cloned from one built on it).
IMG=debian-13-standard_13.6-1_amd64.tar.zst
if pveam list local 2>/dev/null | grep -q "$IMG"; then ok "container template $IMG present"
else pveam update >/dev/null 2>&1; pveam download local "$IMG" >/dev/null 2>&1 \
  && ok "container template $IMG downloaded" || fail "could not download $IMG (no internet or apt mirror?)"; fi

# Outbound access, needed to build the engine template.
curl -sfI -m 8 http://deb.debian.org/debian/ >/dev/null && ok "deb.debian.org reachable" \
  || warn "deb.debian.org not reachable: building the engine template needs it (or an apt mirror)"

# Time sync (corosync, certificates, BGP/BFD timers).
timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes && ok "clock is NTP-synchronized" \
  || warn "clock is not NTP-synchronized"

if [ -n "$SDN" ]; then
  pvesh get /cluster/sdn/zones/lab >/dev/null 2>&1 || pvesh create /cluster/sdn/zones --zone lab --type vlan --bridge "$BRIDGE" >/dev/null
  for pair in $SDN; do
    name=${pair%%:*}; tag=${pair##*:}
    pvesh get "/cluster/sdn/vnets/$name" >/dev/null 2>&1 || pvesh create /cluster/sdn/vnets --vnet "$name" --zone lab --tag "$tag" >/dev/null
  done
  pvesh set /cluster/sdn >/dev/null 2>&1 && ok "SDN zone 'lab' with VNets: $SDN" || fail "applying the SDN configuration failed"
fi

if [ "$TOKEN" = 1 ]; then
  pveum user list --output-format json 2>/dev/null | grep -q '"switchyard@pve"' || pveum user add switchyard@pve >/dev/null
  pveum acl modify / --users switchyard@pve --roles PVEVMAdmin,PVEDatastoreAdmin,PVESDNAdmin,PVEAuditor,PVEPoolAdmin >/dev/null \
    && ok "user switchyard@pve has VM, datastore, SDN, pool and audit rights on /"
  if pveum user token list switchyard@pve --output-format json 2>/dev/null | grep -q '"controller"'; then
    warn "token switchyard@pve!controller already exists (its secret is shown only at creation; remove it with: pveum user token remove switchyard@pve controller)"
  else
    echo "--- API token (shown once) ---"
    pveum user token add switchyard@pve controller --privsep 0 --output-format json
    echo "------------------------------"
    ok "token switchyard@pve!controller created"
  fi
fi

echo
[ "$FAILS" = 0 ] && echo "node ready" || { echo "$FAILS check(s) failed"; exit 1; }
