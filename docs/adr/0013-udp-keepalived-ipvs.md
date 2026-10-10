# 0013. UDP via Keepalived/IPVS: forwarding mode and scope

Status: Accepted
Date: 2026-10-10

## Context

Open-source HAProxy cannot proxy generic UDP (HAProxy Enterprise can), so UDP virtual services (WireGuard, DNS, syslog) go through IPVS driven by Keepalived on the same SEs. The design docs fixed the engine but not the forwarding mode, the allowed fields, the health checks, HA behavior or what happens before the renderer exists. They also disagreed on timing: 01 put UDP in phase 1, 02 put it in M6+. UDP and the WireGuard rebuild are phase 2 (M6).

## Decisions

1. **Phase.** UDP serving is M6. `protocol: udp` stays in the model, validators and port-collision check now.
2. **Forwarding mode: IPVS NAT with source NAT (masquerade) on the SE.** Backends need no change. Direct routing is rejected: it needs the VIP on every backend's loopback with ARP suppression and L2 adjacency, which does not suit a multi-tenant platform that does not control tenant backends. Tunnel mode is rejected for the same reason (backend must terminate the tunnel).
3. **Health checks.** New monitor type `udp` renders `UDP_CHECK`. `MISC_CHECK` is not exposed (it runs a script on the SE); protocol-aware probes (DNS, RADIUS) can come later as fixed agent-provided monitor types. `udp` monitors are valid only on pools used by UDP services, and `tcp`/`http` monitors not on them.
4. **Schedulers.** `roundrobin`→`rr`, `leastconn`→`lc`, `source`→`sh` (`wrr`/`wlc` when member weights differ). `random` is rejected on pools used by UDP services.
5. **HA.** VRRP mode unchanged. In BGP mode Keepalived runs IPVS only, FRR advertises the VIP, and the agent watchdog withdraws the route when Keepalived is down or a UDP service has no healthy real server. `lvs_sync_daemon` is not used in M6.
6. **Allowed fields.** On a UDP service: VIP, port numbers and `default_pool_id` (plus name, enabled, SE group). Everything HTTP, TLS or policy related is rejected with a field-level error. Pools used by UDP services also reject `tls`, `persistence` and `random`.
7. **Validation and status.** The rendered `keepalived.conf` passes `keepalived -t` in a testcontainer (same image version as the SE). The agent reports IPVS real-server state over netlink (fallback `ipvsadm -Ln --stats`) in the HAProxy member shape.
8. **Before M6.** A UDP service is accepted and stored but not served. The diff shows a `warnings[]` entry (`udp_not_served`) and engine status reports `not_served`.

## Consequences

- Backends see the SE address, not the client address, and all UDP traffic crosses the SE in both directions. Fine for WireGuard, DNS and syslog at this scale; revisit if throughput or client IP matters.
- Without connection sync, a VRRP failover or an ECMP rehash can move an in-flight flow to an SE with no table entry. `sh` keeps the same backend; `rr`/`lc` may not. Use `sh` for stateful protocols such as WireGuard.
- The backend needs: the `udp` monitor type, the extra validator rules (listed in the design doc as "still to add"), diff `warnings[]` and `not_served` status, the Keepalived renderer, the nftables masquerade rule and sysctls in the agent bundle, and IPVS status in the agent.
- Avi cannot be decommissioned until WireGuard moves (migration step 8), but Avi's current WireGuard service already cannot carry traffic.

## Alternatives considered

- **Direct routing or tunnel mode:** better throughput and the real client IP, but not workable with tenant-owned backends (see 2).
- **HAProxy Enterprise for UDP:** works, but is paid and lets HAProxy own UDP; rejected to stay on open-source components.
- **A userspace UDP proxy (for example nginx stream):** another engine to render, validate and report on; rejected in favor of the kernel IPVS already in the SE template.
- **`lvs_sync_daemon` now:** needs a VRRP instance and a master/backup split, so it does not fit BGP active/active; deferred.
- **Substituting a scheduler for `random`:** hides a behavior change; rejecting it is clearer.
