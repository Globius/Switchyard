# Load and failure test of the production-style lab (2026-10-14)

A full functional and load test of the three-node Compose controller and its engines, on the two-host Proxmox lab, running the nightly build
`0.1.0-nightly.20261014` plus the fixes listed at the end. The test found four problems in the controller and the roll; all are fixed in the merge
request that carries this page. Earlier runs: [production deployment](../2026-10-13-prod-deploy/index.md),
[upgrades](../2026-10-13-upgrade-lab/index.md).

## What was built for it

| Piece | What |
| --- | --- |
| Controller | 3 privileged LXC nodes (2 vCPU, 4 GB each) over two hosts, hardened database tier, the controller's own VIP on a two-engine SE group |
| Engines | tenant `apps`: SE group of two QEMU engines (2 vCPU, 2 GB), VRRP, one HTTP virtual service, health monitor `GET /health` |
| Backends | two nginx containers (2 vCPU, 1 GB each) serving a 3-byte page, an index page and a 1 MB file |
| Load | two generator containers (2 vCPU, 1 GB each, one per host) running `wrk` and `ab` (installed through the lab's HTTP proxy) |
| Probes | a 100 ms `curl` loop against the VIP, a 200 ms authenticated API read against the controller VIP |

The lab runs on a VXLAN overlay with MTU 1430, so absolute numbers are modest; the values to read are the errors and the windows.

## Data plane

All with two generators of 64 connections unless noted; "0" means no non-2xx response and no socket error.

| Test | Result |
| --- | --- |
| Baseline, small object | 5.3k requests/s together, p99 76 ms, 0 errors |
| 256 connections from one generator | 4.9k requests/s, p99 115 ms, 0 errors |
| 1 MB objects | 139 MB/s from the generator on the engine's host; 23 MB/s from the other host (the overlay, not the engine, is the limit; 4 timeouts and 38 timeouts) |
| Two config applies during 90 s | 465k requests, **0 errors**; each canary-first apply took 16 s |
| MASTER engine powered off (VRRP failover) | 2.3 s window in the probe; 53 reset connections per generator (the ones in flight); 0 non-2xx |
| One backend stopped for 30 s | 0 failed probes; 10 reset connections per generator; traffic moved to the other backend |
| Engine roll (QEMU, two engines) under 540 s of load | 2.5M requests, 0 non-2xx, 128 reset connections, one 2.3 s window at the VRRP switch |
| Controller database leader powered off | the data plane did not notice: 426k requests, 0 errors |
| Controller node powered off | 490k requests, 0 errors |
| **Soak, 10 minutes, periodic config applies (four finished)** | **3.03M requests, 0 errors**; memory of API and worker flat (205 to 212 MB, 127 to 129 MB) |

## Control plane

Measured from a generator through the controller VIP (TLS on the management SE group) or directly at a node's proxy port.

| Test | Before the fixes | After |
| --- | --- | --- |
| UI static page, 64 connections | 3.4k requests/s, p99 67 ms | same |
| `GET /healthz`, one node | 109 requests/s | 165 requests/s |
| `GET /healthz` through the VIP | 315 requests/s | 420 requests/s |
| Authenticated read of the virtual services, one node | 6 requests/s (timeouts at 2 s) | 9 requests/s |
| Writes: 300 `POST /change-sets`, 4 at a time | not measured | 13 requests/s, 0 failures (the drafts were discarded afterwards) |
| ctl-1 (first database host, a proxy pool member) powered off during API reads | **503 for over 70 s** (all three nodes marked down) | 25 s of intermittent timeouts, then normal |
| Database leader powered off during API reads | not measured | 30 s of timeouts, then normal; a Patroni stop (not a power-off) gives 4 s |

The API is CPU-bound in one Python process per node: a read of a tenant's objects costs about 100 ms of CPU (ORM loading and, with the database
on another node over TLS, many round trips), so one node serves about 10 reads a second and the cluster about 30. That is enough for tens of users
of the UI, not for automation hammering the API; scale-out means more than one API process per node. This is design work, not fixed here
(see below).

## Problems found and fixed

1. **A controller node that disappears took the whole controller VIP down.** The middleware that reads the public URL (on every request,
   `/healthz` included) waited on the database with no timeout. With the first host of the connection list gone, a new connection waited about
   two minutes for it before trying the next one, health checks timed out, all three nodes went DOWN and the VIP answered 503 for over a minute.
   The settings are now served from a cache that is refreshed in the background (a request never waits for the database for this, and the
   refresh is one at a time and limited to 2 s), and the production database URL has `connect_timeout=3` and TCP keepalives.
2. **The database pool** was SQLAlchemy's 5 plus 10 overflow: beyond five concurrent requests each one opened and closed a TLS connection to
   PostgreSQL. It is 8 plus 8 now (`SWITCHYARD_DB_POOL_SIZE`, `SWITCHYARD_DB_POOL_OVERFLOW`), with a ping on checkout so a connection to a demoted
   primary is replaced. `max_connections` is 200 in the Patroni defaults for new clusters (an existing one: `patronictl edit-config`).
3. **An engine roll counted a reapply that timed out as done.** The reapply job ended "succeeded" with an error (`ReadTimeout` to the new
   engine) and the roll went on to wait for a health score that could never come. A reapply that did not apply now pauses the roll with the
   error; `resume` repeats it.
4. The earlier roll bug (no reapply after retiring the old engine) is in the same area and was fixed in the upgrade lab MR.

## Not fixed, and how it was left

- **API throughput.** One process per node and about 100 ms of CPU per authenticated read. Ideas: run several API processes per node (the
  proxy already balances to the API service), cache a tenant's snapshot per revision, and make the list endpoints load only the objects they return.
- **Health check speed.** The management SE group takes 25 to 30 s to take a dead node out of rotation (default checks); tighten the monitor
  of the controller pool if that matters.
- **UI.** A tour of 39 pages (every admin page, every tenant page for two tenants, the four tabs of a virtual service) showed no console
  error or failed request except `GSLB`, which answers 404 on three endpoints when no GSLB is configured and shows its empty state.
- The controller cannot fetch the release feed from GitHub inside this lab (github.com has no IPv6 and the hosts have no IPv4). Against a TLS
  mirror inside the lab the fetch, the signature check (nightly key) and the listing worked, and staging an uploaded bundle worked; staging by
  fetching the bundle from the feed needs the real URL.

## Related

[production.md](../../production.md) · [upgrades.md](../../upgrades.md) · [runbooks/rolling-upgrade.md](../../runbooks/rolling-upgrade.md)
