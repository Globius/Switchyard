# ADR 0192: IPVS direct-return and tunnel forwarding for UDP services

Status: Accepted

## Context

UDP services render to IPVS `virtual_server` blocks in Keepalived with `lb_kind NAT` (ADR 0020). NAT sends every reply back
through the SE, which becomes the bottleneck for high-bandwidth UDP such as media or DNS responses. FortiADC, F5 and
NetScaler all offer direct server return. The parity plan lists it as D7.

## Decision

`virtual_service.forward_mode` is `nat` (the default, today's behaviour), `dr` or `tunnel`. It is valid on `udp` services only
(`forward_mode_not_allowed`). Stored in a new `forward_mode` column with a check constraint (migration 0033).

- **`dr`** renders `lb_kind DR`. IPVS rewrites only the MAC address. Each member must be in a `backend` network of the SE
  group, so it shares a layer-2 segment with the SE (`ipvs_dr_not_adjacent`).
- **`tunnel`** renders `lb_kind TUN` (IPIP). No adjacency is required, so members may be routed behind the SE.
- Neither method can change the destination port, so every member must use the service port, and a service with several
  ports needs one pool per port (`ipvs_port_mismatch`).
- Replies bypass the SE, so the masquerade rule and sysctls of `nat` are rendered only while the group has a `nat` UDP
  service (`switchyard-udp.nft` lists only the `nat` members).
- Defaults chosen without asking: `nat` stays the default so nothing changes for existing services; mixed modes in one group
  are allowed; the SE does not configure anything on the members.

## The member side is the operator's job

Switchyard never logs in to members. For `dr` and `tunnel` the member must own the VIP and not answer ARP for it:

- `dr`: VIP on `lo` (`ip addr add <vip>/32 dev lo`), `net.ipv4.conf.all.arp_ignore=1`, `arp_announce=2`
  (IPv6: `ndisc_notify` off and `accept_dad=0` on `lo`).
- `tunnel`: an `ipip` (IPv6: `ip6tnl`) interface up, the VIP on it, `rp_filter=0` on that interface, and a route to the
  client network.

`docs/render.md` has the exact commands. The health check (`UDP_CHECK`) still runs from the SE to the member address.

## Consequences

Return traffic no longer crosses the SE, so the agent's in/out packet counters for DR/TUN services show only inbound traffic.
Packet capture on the SE (ADR 0194) cannot see replies of these services.
