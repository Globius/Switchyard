#!/bin/sh
# Adds the IPv6 side to the lab's helper guest (run as root on the node that holds it; after helper.sh). IPv6 addresses
# ::fa on the three lab networks (fd30:0:0:1..3::/64), the web backend on [fd30:0:0:3::fa]:8001, a UDP echo on port 5301
# and IPv6 multipath in the BGP upstream (AS 65000). Engines are added as neighbors by the scenario, as for IPv4.
#   env: VMID (default 9970)
set -eu
VMID=${VMID:-9970}
for n in 0 1 2; do
  line=$(pct config "$VMID" | sed -n "s/^net$n: //p")
  case "$line" in *ip6=*) ;; *) pct set "$VMID" --net$n "$line,ip6=fd30:0:0:$((n+1))::fa/64" >/dev/null ;; esac
  pct exec "$VMID" -- sh -c "ip -6 addr replace fd30:0:0:$((n+1))::fa/64 dev eth$n"
done
pct exec "$VMID" -- sh -c 'cat > /root/web6.cfg <<CFG
global
    maxconn 100
defaults
    mode http
    timeout client 5s
    timeout server 5s
    timeout connect 5s
frontend f
    bind ipv6@fd30:0:0:3::fa:8001
    http-request return status 200 content-type text/plain string "helper backend (ipv6)"
CFG
[ -f /run/web6.pid ] && kill $(cat /run/web6.pid) 2>/dev/null || true
haproxy -D -f /root/web6.cfg -p /run/web6.pid'
pct exec "$VMID" -- sh -c 'cat > /root/echo6.pl <<PL
use Socket; use IO::Socket::IP;
my \$s = IO::Socket::IP->new(LocalHost => "fd30:0:0:3::fa", LocalPort => 5301, Type => SOCK_DGRAM) or die;
while (1) { \$s->recv(my \$m, 1500); \$s->send(\$m); }
PL
[ -f /run/echo6.pid ] && kill $(cat /run/echo6.pid) 2>/dev/null || true
nohup sh -c "echo \$\$ > /run/echo6.pid; exec perl /root/echo6.pl" >/dev/null 2>&1 &'
pct exec "$VMID" -- sh -c 'vtysh -c "configure terminal" -c "router bgp 65000" -c "address-family ipv6 unicast" -c "maximum-paths 4" -c "exit-address-family" && vtysh -c "write memory" >/dev/null'
echo "helper $VMID has IPv6"
