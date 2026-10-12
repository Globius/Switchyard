# 0119. Weighted pool routes (canary and blue-green)

Status: Accepted
Date: 2026-10-12

## Context

Plan item C2: send part of the traffic of a route to another pool, for canary releases and blue-green moves. The route target was one pool.

## Decisions

1. A route has exactly one target: `pool_id`, `redirect` or the new `pools`, a list of two to eight `{pool_id, weight}` with weights from 1 to 99
   that add up to 100, and no pool twice. The match is whatever the route matches (host, path, and the conditions of the shared match model).
2. Each request draws once (`rand(100)` into `txn.pick`) and the route sends it to the first pool whose cumulative weight is above the draw; the
   last pool takes the rest. The draw is per request, not per client: use persistence on the target pools when a client must stay on one side.
3. A weighted route is always rendered as conditional `use_backend` rules ordered by `order`, never through the host map files, because those hold
   one pool per key.
4. Every place that asks "which pools does this service use" (snapshot slice, bundle, member runtime control, probes, status, impact, backends)
   reads `VsRoute.pool_ids`, so a pool that is only reachable through a weighted route is rendered, checked and monitored. Reference errors point
   at `routes[i].pools[j].pool_id`.
5. `tcp` passthrough routes (ADR 0115) cannot be weighted (`passthrough_route_invalid`).
6. Pool priority groups (the second half of the plan item) are not built: a `backup` member (ADR 0106) already gives failover inside a pool, and
   cross-pool failover needs health-aware routing that HAProxy only offers through `use_backend` conditions on server counts. If needed it gets
   its own ADR.
7. Storage: `vs_route.pool_weights` (JSON, null when unused), and the check constraint becomes "exactly one of `pool_id`, `redirect_location`,
   `pool_weights`" (migration 0028).
