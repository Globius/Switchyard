#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# The scenarios the Docker-hosted lab cannot run, on two real Proxmox hosts. Sourced from run.sh after the controller,
# the templates, the tenant and the helper guest exist. Each section prints what it proves; the unprivileged-container
# section only reports (it is a trial), the others fail the run.
set -eu
cd "$(dirname "$0")"
. ./lib.sh
mkdir -p work

reachable() { api status | getj "print(sum(1 for e in d if e['reachable']))"; }
vrrp_master() { api status | getj "print(next((e['engine'] for e in d if e['agent'] and any(v['state']=='MASTER' for v in e['agent']['vrrp'])), ''))"; }
engine_field() { python -c "import json,sys;print(next(e['$2'] for e in json.load(open('$3'))['engines'] if e['name']=='$1'))"; }

step "VM engines on two hosts (VRRP): one request per host, each from that host's template"
api deploy edge 1 "$FIRST_NODE" "$(template_id vm "$FIRST_NODE")" qemu > work/d1.json
api deploy edge 2 "$SECOND_NODE" "$(template_id vm "$SECOND_NODE")" qemu > work/d2.json
getj "n={e['pve_node'] for e in d['engines']};assert len(d['engines'])==2 and len(n)==2, d" < work/d2.json || fail "the two engines are not on two different nodes"
enroll_all work/d2.json qemu
wait_for "both VM engines reachable" "[ \"\$(reachable)\" = 2 ]" 80
api vs web edge 10.99.0.240 http 80 web > work/vs-web.json
getj "assert d.get('state')=='applied', d" < work/vs-web.json
wait_for "client on the helper gets 200 from the VIP" "vip_ok 10.99.0.240" 20
M=$(vrrp_master); [ -n "$M" ] || fail "no VRRP MASTER"
echo "   MASTER is $M"
MV=$(engine_field "$M" vmid work/d2.json); MN=$(engine_field "$M" pve_node work/d2.json)
gctl qemu "$MN" stop "$MV" >/dev/null
start=$(date +%s)
wait_for "VIP answers after the MASTER's host lost it" "vip_ok 10.99.0.240" 30
echo "   VIP answered again $(( $(date +%s) - start ))s after the MASTER VM stopped (the other engine is on the other host)"
gctl qemu "$MN" start "$MV" >/dev/null
wait_for "both engines reachable again" "[ \"\$(reachable)\" = 2 ]" 80
wait_for "VIP still answers" "vip_ok 10.99.0.240" 20

step "UDP through IPVS (needs ip_vs on the nodes)"
if nssh "$FIRST_NODE" test -e /proc/net/ip_vs && nssh "$SECOND_NODE" test -e /proc/net/ip_vs; then
  api vs echo edge 10.99.0.241 udp 5301 echo > work/vs-echo.json
  getj "assert d.get('state')=='applied', d" < work/vs-echo.json
  REPLY=$(helper perl -MIO::Socket::INET -e 'my $s=IO::Socket::INET->new(PeerAddr=>"10.99.0.241",PeerPort=>5301,Proto=>"udp",Timeout=>3) or die; $s->send("sy-udp-ok"); $s->recv(my $m,100); print $m;')
  [ "$REPLY" = "sy-udp-ok" ] || fail "UDP echo through the IPVS VIP returned '$REPLY'"
  echo "   UDP echo through the VIP works"
else
  echo "   SKIPPED: ip_vs is not loaded on both nodes (run prep-host.sh)"
fi

step "Unprivileged containers (a trial: reports, never fails the run)"
api deploy edge-unpriv 1 "$FIRST_NODE" "$(template_id unpriv "$FIRST_NODE")" lxc > work/du.json
enroll_all work/du.json lxc
if wait_for "unprivileged engine reachable" "[ \"\$(api status | getj \"print(sum(1 for e in d if e['engine']=='edge-unpriv-1' and e['reachable']))\")\" = 1 ]" 40 2>/dev/null; then
  api vs unpriv edge-unpriv 10.99.0.242 http 80 web > work/vs-unpriv.json
  getj "print('   applied' if d.get('state')=='applied' else '   NOT applied: '+str(d.get('error')))" < work/vs-unpriv.json
  vip_ok 10.99.0.242 && echo "   unprivileged: HAProxy and VRRP serve the VIP" || echo "   unprivileged: the VIP does not answer"
else
  echo "   unprivileged: the engine never became reachable (see the container's journal)"
fi

step "BGP with ECMP: three engines over both hosts, one upstream router"
api deploy edge-bgp 2 "$FIRST_NODE" "$(template_id lxc "$FIRST_NODE")" lxc > work/db1.json
api deploy edge-bgp 3 "$SECOND_NODE" "$(template_id lxc "$SECOND_NODE")" lxc > work/db2.json
for ip in $(getj "print(' '.join(e['mgmt_ip'] for e in d['engines']))" < work/db2.json); do
  helper vtysh -c "configure terminal" -c "router bgp 65000" -c "neighbor $ip remote-as 65100" -c "neighbor $ip bfd" \
    -c "address-family ipv4 unicast" -c "neighbor $ip activate"
done
enroll_all work/db2.json lxc
wait_for "three BGP engines reachable" "[ \"\$(api status | getj \"print(sum(1 for e in d if e['engine'].startswith('edge-bgp') and e['reachable']))\")\" = 3 ]" 60
api vs bgpweb edge-bgp 10.99.0.230 http 80 web > work/vs-bgp.json
getj "assert d.get('state')=='applied', d" < work/vs-bgp.json
nexthops() { helper vtysh -c "show ip route 10.99.0.230/32" | grep -c ', via '; }
wait_for "the upstream has three equal-cost paths" "[ \"\$(nexthops)\" = 3 ]" 30
wait_for "client reaches the VIP over BGP" "vip_ok 10.99.0.230" 20
B1=$(getj "e=next(e for e in d['engines'] if e['name']=='edge-bgp-1');print(e['pve_node'], e['vmid'])" < work/db2.json)
gexec lxc ${B1% *} ${B1#* } systemctl stop haproxy
wait_for "the upstream drops to two paths when one engine's HAProxy stops" "[ \"\$(nexthops)\" = 2 ]" 30
gexec lxc ${B1% *} ${B1#* } systemctl start haproxy
wait_for "three paths again" "[ \"\$(nexthops)\" = 3 ]" 30

echo "HOSTS LAB PASSED: VM engines on two hosts, VRRP failover across hosts, UDP/IPVS (when available), ECMP with three BGP engines"
