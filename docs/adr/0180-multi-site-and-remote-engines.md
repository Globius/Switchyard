# 0180. Multi-site: sites, remote engines over an outbound channel, per-site DNS and probes

Status: Proposed. Later phase, low priority; nothing is built by this ADR.
Date: 2026-10-12

## Context
GSLB ([0160](0160-gslb-on-powerdns.md) to [0165](0165-gslb-latency-selection.md)) answers across sites, but a "site" is only a
free label on a wide IP member. Question: what does it take to run engines and members in other clouds (OVH, AWS, Azure, GCP,
Hetzner)?

### How it works today
- **GSLB.** A wide IP is one PowerDNS `LUA` record set. A member is an `address` or a `virtual_service` (its VIP, followed when it
  moves). Health is PowerDNS `ifportup`/`ifurlup`, run from each PowerDNS server's own address. The controller writes records to
  one PowerDNS API; other sites are secondaries ([gslb.md](../gslb.md)). `latency` is measured from the controller only.
- **Engine connection model.** The engine enrolls outbound (`POST /enroll` over https, 8443). After that the **controller dials the
  engine**: gRPC over mTLS to `sy-agent` (7443) and the Data Plane API (5556) on the engine's registered management address.
- **Members in other clouds.** A plain `address` member works now: PowerDNS health-checks any routable address.

### What does not work
1. **Managed engines in another cloud.** The controller needs an inbound path to 7443 and 5556: public exposure or an operator-built
   VPN. Engines behind NAT cannot be managed.
2. **Deploying them.** `deploy_se_group` only knows Proxmox. Remote engines must be created by hand and enrolled with a token.
3. **VIP placement.** BGP/VRRP assume a fabric we control; clouds give an elastic IP, not a BGP session or gratuitous ARP.
4. **Vantage points.** No object says which PowerDNS answers for which site, and there is no per-site health view.
5. **Partial failure.** Apply and status assume the whole fleet is reachable; one slow or partitioned site should not stall the others.

## Decision (plan, later phase)
1. **Site object.** `site` (`name`, `kind` proxmox|external, `provider`, `region`, `dns_servers[]`). `engine.site_id` and
   `se_group.site_id`; wide IP members point at `site_id` (the label stays as fallback). Existing Proxmox clusters become default
   sites in the migration. One virtual service per site, tied together by the wide IP; no cross-site service object.
2. **Remote engines connect out.** Engine mode `connect: out`. `sy-agent` holds an outbound mTLS connection (same agent
   certificate, TCP 8444) to a controller relay and multiplexes the agent and Data Plane API streams back over it. Only outbound
   8444 is needed; NAT and cloud security groups are fine. The agent contract and its limits (writes only under
   `/etc/switchyard/`, no remote exec) do not change. Operator-managed WireGuard stays an option (mgmt address on the tunnel, mode
   `in`). SEs keep serving when the stream is down.
3. **Per-site apply.** Canary and rollout run inside each site, sites in sequence; an unreachable site is `pending`, not failed, and
   does not block the others. Drift is still only reported.
4. **DNS placement.** One PowerDNS (behind dnsdist) per site, at least two sites answering each zone. Default: the controller
   writes the same records to every site's PowerDNS API through the relay; AXFR secondaries stay supported. NS/DS at the parent
   remain manual.
5. **Several vantage points.** Wide IP `vantage: local` (today) or `consensus`: probe-only mode in `sy-agent` (allow-listed TCP/HTTP
   checks, no exec) reports per site; a member is up if a quorum of sites sees it up. Controller-supplied health is advisory and
   expires, so DNS keeps answering with the controller down (0160). `status` gains per-member, per-site health.
6. **Cloud adapters** (OpenTofu, one ADR per provider) are separate follow-ups. A cloud VIP is the provider's elastic IP
   (`routing: none`).

## Consequences
Managed engines in any cloud need only outbound 8444. Cost: a relay, a transport abstraction in the controller clients, per-site
apply and status. Migration is additive (`connect: in` stays the default).

## Alternatives considered
Expose 7443 and 5556 publicly (surface, no NAT); SSH or a cloud agent from the controller (CLAUDE.md forbids); WireGuard only (key
management falls on operators); one controller per site (valid deployment, loses one desired state); managed global DNS (breaks
the self-hosted constraint).

## Open questions
Multiplexer (yamux, HTTP/2 or a gRPC bidirectional stream); whether pushes should move to the agent so the Data Plane API stays
on loopback; quorum default.
