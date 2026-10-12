#!/bin/sh
# Optional internet access for the lab's management networks, from the node that holds their gateway addresses (run as root
# on it). Engines boot without internet; this is for operators who want to apt-get or curl from a lab guest.
#   IPv4 network: an HTTP(S) proxy (tinyproxy) on the gateway address. The node may have no IPv4 at all, so a plain NAT is
#                 impossible; the proxy resolves names and connects over IPv6. Guests use http_proxy=http://GATEWAY:8888.
#   IPv6 network: NAT66 (masquerade) from the lab ULA prefix out of the node's default interface; guests also need a
#                 nameserver they can reach.
#   env: V4_GATEWAY (default 172.30.0.1), V4_NET (172.30.0.0/24), V6_NET (fd30:0:0:1::/64, empty to skip)
set -eu
V4_GATEWAY=${V4_GATEWAY:-172.30.0.1}; V4_NET=${V4_NET:-172.30.0.0/24}; V6_NET=${V6_NET-fd30:0:0:1::/64}
DEBIAN_FRONTEND=noninteractive apt-get install -y tinyproxy >/dev/null
sed -i '/^Listen /d; /^Allow /d; s/^Port .*/Port 8888/' /etc/tinyproxy/tinyproxy.conf
printf 'Listen %s\nAllow %s\n' "$V4_GATEWAY" "$V4_NET" >> /etc/tinyproxy/tinyproxy.conf
systemctl enable tinyproxy >/dev/null 2>&1; systemctl restart tinyproxy
if [ -n "$V6_NET" ]; then
  sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null
  nft delete table ip6 sy_lab_nat 2>/dev/null || true
  nft add table ip6 sy_lab_nat
  nft "add chain ip6 sy_lab_nat post { type nat hook postrouting priority srcnat; }"
  nft add rule ip6 sy_lab_nat post ip6 saddr "$V6_NET" oifname != '"lab*"' masquerade
  echo "NAT66 for $V6_NET is on (not persistent: rerun after a reboot)"
fi
echo "proxy: http://$V4_GATEWAY:8888 (allowed: $V4_NET)"
