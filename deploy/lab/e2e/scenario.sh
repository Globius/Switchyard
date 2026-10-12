#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# The end-to-end scenario, on top of base.sh. Two VRRP engines (data NICs addressed from pools), canary ordering,
# failover and rejoin, a controller-less reboot, then a BGP group peering with an upstream FRR router.
set -eu
cd "$(dirname "$0")"
. ./lib.sh
getj() { python -c "import json,sys;d=json.load(sys.stdin);$1"; }
vmid_of() { getj "print(next(e['vmid'] for e in d['engines'] if e['name']=='$1'))" < "$2"; }
engine_state() { api status | getj "print(next((e for e in d if e['engine']=='$1'), {}).get('$2'))"; }
vrrp_of() { api status | getj "e=next(e for e in d if e['engine']=='$1');print((e['agent'] or {'vrrp':[{'state':'-'}]})['vrrp'][0]['state'] if (e['agent'] or {}).get('vrrp') else '-')"; }
vip_ok() { [ "$(docker exec $PVE curl -s -m 2 -o /dev/null -w '%{http_code}' "http://$1/api/v1/healthz")" = 200 ]; }

step "two VRRP engines through the controller; data NICs get addresses from their pools"
api deploy edge 2 > work/deploy-edge.json
getj "assert len(d['engines'])==2 and all(e['guest_kind']=='$GUEST_KIND' for e in d['engines']), d" < work/deploy-edge.json
E1=$(vmid_of edge-1 work/deploy-edge.json); E2=$(vmid_of edge-2 work/deploy-edge.json)
A1=$(nic_ip "$E1" 1); A2=$(nic_ip "$E2" 1)
[ -n "$A1" ] && [ -n "$A2" ] && [ "$A1" != "$A2" ] || fail "VIP-network NIC addresses missing or equal: '$A1' '$A2'"
echo "   edge-1 eth1 $A1, edge-2 eth1 $A2"
sh enroll.sh work/deploy-edge.json >/dev/null
wait_for "both engines reachable" "[ \"\$(api status | getj \"print(sum(e['reachable'] for e in d))\")\" = 2 ]" 40

step "apply: the canary engine takes the change first, then the rest"
api vs web edge 10.99.0.240 > work/vs-web.json
getj "assert d.get('state')=='applied', d" < work/vs-web.json
api lastapply > work/lastapply.json
getj "r=d['job']['result']['groups'][0]['details'];assert r['canary']=='edge-1', r;assert sorted(r['engines'])==['edge-1','edge-2'], r;assert len(set(r['bundles'].values()))==2, r
stages=[l['message'] for l in d['log']['lines'] if l['message'].endswith(': started')]
assert stages.index('canary: started') < stages.index('rollout: started'), stages" < work/lastapply.json
wait_for "200 through the VIP" "vip_ok 10.99.0.240"
wait_for "edge-1 MASTER" "[ \"\$(vrrp_of edge-1)\" = MASTER ]"
[ "$(vrrp_of edge-2)" = BACKUP ] || fail "edge-2 is not BACKUP"

step "failover: the MASTER dies, the VIP moves, and the old MASTER takes it back"
start=$(date +%s)
gstop "$E1" >/dev/null
wait_for "VIP answers after edge-1 died" "vip_ok 10.99.0.240" 20
echo "   VIP answered again $(( $(date +%s) - start ))s after the MASTER stopped"
gexec "$E2" ip -4 -o addr show eth1 | grep -q 10.99.0.240 || fail "edge-2 does not hold the VIP"
wait_for "controller sees edge-1 down, edge-2 MASTER" "[ \"\$(engine_state edge-1 reachable)\" = False ] && [ \"\$(vrrp_of edge-2)\" = MASTER ]" 40
gstart "$E1" >/dev/null
wait_for "edge-1 holds the VIP again (higher priority preempts)" "gexec $E1 ip -4 -o addr show eth1 | grep -q 10.99.0.240" 40
wait_for "VIP still answers" "vip_ok 10.99.0.240"

step "the controller is gone and both engines reboot: they still serve"
docker stop sy-e2e-api sy-e2e-worker sy-e2e-tls >/dev/null
greboot "$E1" >/dev/null; greboot "$E2" >/dev/null
sleep 10
wait_for "VIP answers with the controller down, after reboots" "vip_ok 10.99.0.240" 40
docker start sy-e2e-api sy-e2e-worker sy-e2e-tls >/dev/null
wait_for "controller back" "docker exec sy-e2e-api python -c \"import urllib.request as u;u.urlopen('http://127.0.0.1:8000/api/v1/healthz',timeout=2)\"" 20
wait_for "both engines reachable again" "[ \"\$(api status | getj \"print(sum(e['reachable'] for e in d))\")\" = 2 ]" 40

if [ "$GUEST_KIND" != lxc ]; then
  echo "E2E PASSED ($GUEST_KIND engines): addressed data NICs, canary order, VRRP failover and rejoin, controller-less reboot (the BGP part runs for lxc)"
  exit 0
fi
step "BGP: an upstream FRR router, a BGP group with one engine, route announced and withdrawn"
pct clone $TEMPLATE 9970 --full --hostname upstream >/dev/null 2>&1
pct set 9970 --net0 name=eth0,bridge=vmbr0,ip=172.30.0.250/24,gw=172.30.0.1
pct start 9970
docker cp upstream-frr.conf $PVE:/root/upstream-frr.conf
wait_for "upstream container up" "pct exec 9970 -- true" 20
pct push 9970 /root/upstream-frr.conf /etc/switchyard/frr/frr.conf
pct exec 9970 -- systemctl start frr
api deploy edge-bgp 1 > work/deploy-bgp.json
B1=$(vmid_of edge-bgp-1 work/deploy-bgp.json)
M1=$(getj "print(next(e['mgmt_ip'] for e in d['engines'] if e['name']=='edge-bgp-1'))" < work/deploy-bgp.json)
M2=$(getj "print(next(e['mgmt_ip'] for e in d['engines'] if e['name']=='edge-1'))" < work/deploy-edge.json)
[ "$M1" != "$M2" ] || fail "two groups got the same management address $M1"
# Explicit neighbor with BFD (a dynamic listen-range neighbor never brings BFD up, so the engine's session would stay Idle).
pct exec 9970 -- vtysh -c "configure terminal" -c "router bgp 65000" -c "neighbor $M1 remote-as 65100" -c "neighbor $M1 bfd" -c "address-family ipv4 unicast" -c "neighbor $M1 activate"
sh enroll.sh work/deploy-bgp.json >/dev/null
wait_for "BGP engine reachable" "[ \"\$(engine_state edge-bgp-1 reachable)\" = True ]" 40
api vs bgpweb edge-bgp 10.99.0.230 > work/vs-bgp.json
getj "assert d.get('state')=='applied', d" < work/vs-bgp.json
wait_for "BGP session Established on the upstream" "pct exec 9970 -- vtysh -c 'show bgp neighbors $M1' | grep -q 'BGP state = Established'" 20
wait_for "upstream learned the VIP" "pct exec 9970 -- vtysh -c 'show ip route 10.99.0.230/32' | grep -q '$M1,'" 20
wait_for "traffic from the upstream router reaches the VIP over BGP" "pct exec 9970 -- curl -s -m 3 -o /dev/null -w '%{http_code}' http://10.99.0.230/api/v1/healthz | grep -q 200" 20
pct exec "$B1" -- systemctl stop haproxy
wait_for "route withdrawn when HAProxy is down" "! pct exec 9970 -- vtysh -c 'show ip route 10.99.0.230/32' | grep -q '$M1,'" 40
pct exec "$B1" -- systemctl start haproxy
wait_for "route back when HAProxy is healthy again" "pct exec 9970 -- vtysh -c 'show ip route 10.99.0.230/32' | grep -q '$M1,'" 40

step "ECMP: two more BGP engines; the upstream balances over three paths"
api deploy edge-bgp 3 > work/deploy-bgp3.json
for ip in $(getj "print(' '.join(e['mgmt_ip'] for e in d['engines'] if e['name'] != 'edge-bgp-1'))" < work/deploy-bgp3.json); do
  pct exec 9970 -- vtysh -c "configure terminal" -c "router bgp 65000" -c "neighbor $ip remote-as 65100" -c "neighbor $ip bfd" -c "address-family ipv4 unicast" -c "neighbor $ip activate"
done
sh enroll.sh work/deploy-bgp3.json >/dev/null
wait_for "three BGP engines reachable" "[ \"\$(api status | getj \"print(sum(1 for e in d if e['engine'].startswith('edge-bgp') and e['reachable']))\")\" = 3 ]" 60
api vs bgpweb2 edge-bgp 10.99.0.231 > work/vs-bgp2.json     # a new apply rolls the config out to the new engines too
getj "assert d.get('state')=='applied', d" < work/vs-bgp2.json
nexthops() { pct exec 9970 -- vtysh -c "show ip route 10.99.0.231/32" | grep -c ', via '; }
wait_for "the upstream has three equal-cost paths to the VIP" "[ \"\$(nexthops)\" = 3 ]" 30
wait_for "traffic from the upstream router reaches the ECMP VIP" "pct exec 9970 -- curl -s -m 3 -o /dev/null -w '%{http_code}' http://10.99.0.231/api/v1/healthz | grep -q 200" 20
B3=$(getj "print(next(e['vmid'] for e in d['engines'] if e['name']=='edge-bgp-3'))" < work/deploy-bgp3.json)
pct exec "$B3" -- systemctl stop haproxy
wait_for "the upstream drops to two paths when one engine's HAProxy stops" "[ \"\$(nexthops)\" = 2 ]" 40
pct exec "$B3" -- systemctl start haproxy
wait_for "three paths again" "[ \"\$(nexthops)\" = 3 ]" 40

echo "E2E PASSED: addressed data NICs, canary order, VRRP failover and rejoin, controller-less reboot, BGP announce and withdraw, ECMP over three engines"
