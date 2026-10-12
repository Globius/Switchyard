# IPv6 and dual-stack

Switchyard supports IPv6-only and dual-stack deployments. This is the support matrix (ADR 0078).
"Verified" means a test or the real binary checks it.

| Area | IPv4 | IPv6 | Notes |
| --- | --- | --- | --- |
| Data model (VIPs, pools, networks, members, BGP peers, static routes) | yes | yes | `ipaddress` types; a VIP must sit in a network of its own family; routes need a next hop of their own family. |
| IPAM (pool allocation) | yes | yes | Network and broadcast addresses are only excluded for IPv4 prefixes shorter than /31. |
| HAProxy binds, server targets | yes | yes | `ipv6@addr:port` (golden case `10-ipv6`, validated by `haproxy -c`). |
| Keepalived VRRP | yes | yes | One `vrrp_instance` per family: `sy_<group>` for IPv4 and `sy_<group>_6` (VRRPv3) for IPv6, same VRID and priority. Syntax checked with Keepalived 2.3. |
| IPVS UDP services | yes | yes | A UDP service's members must be in the VIP's family; nftables masquerade rules exist for both. The agent parses `[addr]:port`. |
| FRR / BGP | yes | yes | The route-health watchdog file lists v4 (/32) and v6 (/128) VIPs, UDP ones included (golden `07-bgp-v6-only`). Separate prefix lists, route maps and address families; peers may be v4 or v6 (golden `03-bgp-dual-stack`). |
| BGP router id | IPv4 mgmt address | derived | An IPv6-only engine has no IPv4 address; its router id is derived from the engine name inside `100.64.0.0/10`, stable across renders. |
| Engine address policy, dataplane/agent URLs | yes | yes | Literal IPs checked against the group's mgmt networks; `[addr]:port` accepted. |
| Monitor test targets | yes | yes | Reported as `[addr]:port`. |
| Proxmox adapter, deploy job | yes | yes | `ip=`/`gw=` or `ip6=`/`gw6=` per NIC (VM `ipconfigN`, container `netN`); engine URLs bracket IPv6 literals. A management NIC carries one family. |
| SE image | yes | yes | IPv6 sysctls (`ip_nonlocal_bind`, forwarding, `accept_ra=2`), `nftables` enabled; agent reads IPv6 BGP peers and withdraws IPv6 VIPs (ADR 0079). |
| Quickstart | yes | yes | `bootstrap.sh --host A,B --ipv6`; certificate names for every address; the front listens on `[::]`; Docker network with NAT66. Needs Docker IPv6 NAT enabled (Docker 26). |
| Lab | yes | yes | Verified on two IPv6-only Proxmox hosts with an IPv4-free tenant: VRRPv3 across hosts, IPVS UDP, BGP ECMP, telemetry ([runbooks/lab-hosts.md](runbooks/lab-hosts.md)). |

Limits: a management NIC carries one address (IPv4 or IPv6, not both); a UDP service cannot translate
between families (IPVS limit); a VRRP group with both families needs both VIP networks on the group.

Known gaps: the BGP renderer lists only IPv4 prefixes in `frr/vips.json`, so the route-health watchdog does not withdraw an
IPv6 VIP when HAProxy fails. An IPv6-only host needs outbound IPv4 for nothing in this repository, but several upstream
sites (ghcr.io, github.com release downloads) are IPv4-only; the controller image installs uv from PyPI for that reason and
engine binaries are built on a machine that reaches them.
