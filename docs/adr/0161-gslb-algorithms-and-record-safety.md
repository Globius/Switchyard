# 0161. GSLB algorithms, health checks and keeping tenant input out of Lua

Status: Accepted
Date: 2026-10-12

## Context
A wide IP renders to a Lua expression that PowerDNS evaluates for every query. Tenant input that reaches Lua unvalidated
is code execution in the DNS server, which every tenant shares.

## Decision
- **Algorithms** (`algorithm`): `failover` (members grouped by `priority`, the first tier with a member up answers;
  `ifportup` with nested tables), `all` (every member that is up, multivalue), `weighted` (`pickwrandom` over the members
  that are up, weight 0 never answers; if all are down every member stays eligible, since an answer is better than none),
  `subnet` (`view`: a member with `subnets` answers clients in those blocks, members without them are the default),
  `geo` (`ifportup` with `selector='pickclosest'`; the PowerDNS server needs a GeoIP database configured by the operator).
  Latency-based selection is not offered: PowerDNS has no measurement for it.
- **Health**: `tcp` (a connect to `port`) or `http` (`scheme`, `path`, optional `match` text and `host`), plus `timeout_ms`
  (default 1000). Both families (IPv4 and IPv6) go in the same record set, as an `A` and an `AAAA` Lua record.
- **Only validated tokens reach Lua.** Addresses are parsed as IP literals and re-printed, ports, weights, priorities and
  TTLs are integers in fixed ranges, subnets are parsed CIDR blocks, `path` has a fixed alphabet with no quote, `match` is
  letters, digits and `space . _ -`, `host`, zone, label, site and service names are lowercase DNS-safe words. Unknown fields
  are rejected. The expression is assembled from these and from constants in `domain/gslb.py`; there is no free-form
  expression, no function name and no snippet from the tenant. Tests feed injection attempts (quotes, `})`, `os.execute`) to
  every field.
- **Limits**: 32 members per wide IP, TTL 5 to 3600 s (default 30), one `LUA` record set per name.

## Consequences
Every algorithm has a unit test of the exact record and, with `SWITCHYARD_STACK_TESTS=1`, a test against a real PowerDNS
that asks it questions over UDP. Adding an algorithm means adding it to both. `geo` renders but cannot be proven without a
GeoIP database, so it is covered by the unit test only.

## Alternatives considered
Accepting a Lua snippet per wide IP (the flexibility is exactly the risk); building records only from `A` sets and
health-driven enable/disable by the controller (health from the wrong place and a dependency on the controller).
