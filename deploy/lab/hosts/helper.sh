#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Creates the lab's helper guest on THIS Proxmox node (run as root on the node). One container with an address on each
# lab network (.250) that plays: the client that requests the VIPs, the web backend (TCP 8001) and UDP echo (5301)
# on the backend network, and the upstream BGP router (AS 65000) on the management network.
#   env: TEMPLATE (container template id), VMID (default 9970), VNET_MGMT, VNET_VIP, VNET_BACKEND
set -eu
: "${TEMPLATE:?}" "${VNET_MGMT:?}" "${VNET_VIP:?}" "${VNET_BACKEND:?}"
VMID=${VMID:-9970}
if pct status "$VMID" >/dev/null 2>&1; then echo "helper $VMID exists"; exit 0; fi
pct clone "$TEMPLATE" "$VMID" --full --hostname helper >/dev/null
pct set "$VMID" --net0 "name=eth0,bridge=$VNET_MGMT,ip=172.30.0.250/24,gw=172.30.0.1" \
  --net1 "name=eth1,bridge=$VNET_VIP,ip=10.99.0.250/24" --net2 "name=eth2,bridge=$VNET_BACKEND,ip=10.98.0.250/24"
pct start "$VMID"
n=0; until pct exec "$VMID" -- true 2>/dev/null; do n=$((n+1)); [ $n -lt 30 ] || { echo "helper did not start" >&2; exit 1; }; sleep 2; done

# Web backend: HAProxy answering 200 on the backend address (the engines' HTTP health check and the client's request).
pct exec "$VMID" -- sh -c 'cat > /root/web.cfg <<CFG
global
    maxconn 100
defaults
    mode http
    timeout client 5s
    timeout server 5s
    timeout connect 5s
frontend f
    bind 10.98.0.250:8001
    http-request return status 200 content-type text/plain string "helper backend"
CFG
haproxy -D -f /root/web.cfg -p /run/web.pid'

# UDP echo for the IPVS scenario (perl is always present).
pct exec "$VMID" -- sh -c 'cat > /root/echo.pl <<PL
use IO::Socket::INET;
my \$s = IO::Socket::INET->new(LocalAddr => "10.98.0.250", LocalPort => 5301, Proto => "udp") or die;
while (1) { \$s->recv(my \$m, 1500); \$s->send(\$m); }
PL
nohup perl /root/echo.pl >/dev/null 2>&1 &'

# BGP upstream: accepts engines as explicit neighbors with BFD (added per engine by the scenario), ECMP up to 4 paths.
pct exec "$VMID" -- sh -c 'cat > /etc/switchyard/frr/frr.conf <<CFG
frr defaults traditional
hostname helper
log syslog informational
!
router bgp 65000
 bgp router-id 172.30.0.250
 no bgp ebgp-requires-policy
 no bgp default ipv4-unicast
 bgp bestpath as-path multipath-relax
 !
 address-family ipv4 unicast
  maximum-paths 4
 exit-address-family
!
CFG
systemctl start frr'
echo "helper $VMID ready"
