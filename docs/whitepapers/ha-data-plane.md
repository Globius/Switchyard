# High availability and the data plane

How traffic keeps flowing when an engine, a host or the controller fails, and how changes reach engines without risking
all of them at once. Measured results are in [docs/lab](../lab/2026-10-12-full-pass.md).

## Rule one: the data plane does not depend on the controller

Every engine holds a complete local configuration: HAProxy, FRR or Keepalived, certificates and sidecar files. Failover
between engines (VRRP elections, BGP withdrawal), health checks, persistence, WAF, rate limits and logins all run on the
engines. The controller is needed only to change configuration, deploy or retire engines and collect status. The end-to-end
lab stopped the controller and rebooted engines: the VIP kept serving ([ADR 0056](../adr/0056-end-to-end-lab.md)). GSLB
follows the same rule: PowerDNS answers and health-checks on its own.

## Placing VIPs

Each SE group chooses one HA mode.

**VRRP (Keepalived).** One engine owns each VIP and answers ARP or neighbour discovery for it; a backup takes over when the
owner stops advertising. Simple, works on any L2 network, active/standby per VIP. Failover in the two-host lab took 4 to 5
seconds, IPv4 and IPv6 (VRRPv3, one instance per address family).

**BGP (FRR).** Every healthy engine announces the VIP as a host route to upstream routers, which spread flows across them with
ECMP; BFD detects a dead engine quickly. Active/active and scales out by adding engines. The agent's watchdog withdraws an
engine's announcement when HAProxy (or Keepalived, for UDP) on it stops serving, so the routers stop sending traffic there.
Engines accept no routes from neighbours, and prefix limits guard against a broken upstream.

UDP services run in IPVS (NAT with source NAT) under Keepalived in both modes; with BGP, FRR announces and Keepalived only
balances.

## Inside an engine

- Health monitors (TCP, HTTP, protocol presets, UDP) take members out; passive checks react to live errors; slow start
  ramps a recovered member up; backup members and fallback pools take over when a pool is empty.
- Persistence tables can be shared between the engines of a group (HAProxy peers), so a VRRP failover keeps sessions.
- HAProxy reloads without dropping established connections; the Data Plane API waits for the new worker before the pipeline
  moves on.

## Changing configuration safely

1. Render and validate with the real binaries before anything is sent.
2. Push to **one canary engine**, watch it for `canary_seconds` (default 30): HAProxy process, routing sessions, agent status.
3. Only then the rest of the group. A failure stops the rollout and returns the canary to its previous files.
4. The result is a revision; rollback is a new apply of an old revision through the same steps.

Applies are serialised per SE group. A group is never pushed to all engines at once, including on re-apply after drift and
on rollback.

## Replacing and upgrading engines

Engines are disposable. New HAProxy, FRR, Keepalived or agent versions arrive by building a new template and replacing
engines one at a time, each gated on the group's health score
([rolling upgrade](../runbooks/rolling-upgrade.md)); in the lab a three-engine group was replaced across both hosts while
the VIP failed 2 of 244 probes. Scale-out is a deploy plus a re-apply.

## Controller HA

Three controller nodes, each running the API, the worker, PostgreSQL under Patroni with etcd, and the UI proxy; one
synchronous standby means a committed change survives a node loss. Scheduled jobs are led by whichever worker holds a
PostgreSQL advisory lock. Backups are pgBackRest with point-in-time restore. See the [production guide](../production.md).

## Known limits

- UDP flows are not synchronised between engines; a failover resets them.
- Rate-limit counters and caches are per engine.
- BGP and IPVS inside unprivileged containers are not proven.
- Multi-site with remote engines is proposed only ([ADR 0180](../adr/0180-multi-site-and-remote-engines.md)); today
  separate sites are tied together with GSLB.
