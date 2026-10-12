# 0160. GSLB on PowerDNS: the controller decides, PowerDNS answers and checks

Status: Accepted
Date: 2026-10-12

## Context
One site per controller is the largest gap in the parity plan (08: cross-site failover, D1). A *wide IP* is a DNS name
whose answer depends on which of its members, usually the VIPs of the same service at different sites, are healthy.
Avi calls it GSLB, F5 GTM. 01 chose PowerDNS for this and 08 asked for priority, weighted, geo, subnet, multivalue and
DNSSEC, with the wide IP as a tenant object.

## Decision
- **PowerDNS Authoritative with LUA records** answers. A wide IP is one `LUA` record set whose Lua expression uses
  PowerDNS's own `ifportup` / `ifurlup` (health checks, run by PowerDNS from its own address, which is the vantage point
  that matters for DNS), `pickwrandom` and `view`. No health state is copied into the controller and no daemon of ours sits
  in the answer path: if the controller is down the DNS keeps answering and keeps checking
  (CLAUDE.md: the data plane never depends on the controller).
- **The controller owns what the records say**, and writes them through PowerDNS's HTTP API
  (`adapters/powerdns.py`, with a fake for tests). One request replaces the record set, so a change is atomic.
- **Wide IPs and zones are tenant objects with RLS** (`gslb_zone`, `gslb_wide_ip`), changed live (no change set) and
  audited, like alert targets: DNS changes are cheap, reversible by editing again and not an SE rollout, so the canary
  step does not apply. Tenant administrators write, any role reads.
- **A member is an address or a virtual service.** A service member's VIP is read from desired state whenever the records
  are rendered, so moving a VIP moves the answer. The job `sync_gslb` (every 5 minutes) rewrites a wide IP only when what it
  would write changed (a hash), and retries pushes that failed.
- **Drift is reported, not fixed.** `GET /tenants/{t}/gslb/status` compares each wide IP with PowerDNS (`in_sync`, `drift`,
  `pending`, `error`, `off`). The job never rewrites a record that merely differs; an explicit `POST .../gslb/sync` does.
- **Sites** are a label on a member (`site: pve1`); nothing in the model assumes more than the DNS answer. For tests, each
  Proxmox host of the lab is a site (docs/runbooks/gslb-lab.md).
- Off unless `SWITCHYARD_POWERDNS_URL` and `SWITCHYARD_POWERDNS_API_KEY` are set; routes answer `404 not_enabled`.

## Consequences
- Health is whatever PowerDNS can reach. A member that is up for the DNS server but not for clients (or the reverse) is
  not detected; use an `http` check on the real path for that.
- Newly published members are answered before their first health check completes (PowerDNS assumes up), and answers are
  cached for the record TTL (default 30 s), so a change takes up to a TTL to be seen. Real-PowerDNS tests allow for both.
- The API key can write any zone on that PowerDNS. One PowerDNS is shared by tenants; a tenant's reach is limited by the
  controller (zones belong to one tenant, names are validated), not by PowerDNS.
- LUA records need `enable-lua-records=yes`; a PowerDNS without it answers NXDOMAIN for the names.

## Alternatives considered
dnsdist or CoreDNS in front with controller-fed health (a second state machine to keep right); health in the controller
with plain A records (answers depend on the controller being up, and its vantage point is wrong); Route 53 style managed
DNS (breaks the self-hosted constraint); wide IPs inside change sets (adds a canary that has no meaning for DNS).
