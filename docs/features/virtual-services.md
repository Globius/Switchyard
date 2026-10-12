# Virtual services and routing

**What.** A virtual service is a VIP, one or more ports and what to do with the traffic. `protocol` is `http` (HAProxy at L7,
with host and path routing), `tcp` (HAProxy at L4) or `udp` (Keepalived and IPVS, see [UDP services](udp.md)). It belongs to
one SE group, which decides the engines that serve it.

![A virtual service: routes, live members and metrics](../assets/screenshots/virtual-service.png)

**How to use.**

```yaml
name: shop
protocol: http
se_group_id: <group id>
vip: 192.0.2.10            # or take one from a VIP pool: POST .../vip-pools/{id}/next-free
ports:
  - {port: 80, https_redirect_port: 443}
  - {port: 443, tls: true, http2: true}
certificate_ids: [<certificate id>]   # the first is the default for unknown SNI
default_pool_id: <pool id>
routes:
  - {order: 10, match: {path_prefix: /api}, pool_id: <api pool>}
  - {match: {host: static.example.test}, pool_id: <static pool>}
  - order: 20
    match: {path_prefix: /}
    pools: [{pool_id: <blue>, weight: 90}, {pool_id: <green>, weight: 10}]   # canary split
```

- **Routes** match on host, path prefix or exact path, methods, headers, query parameters, source networks and SNI; every
  condition given must hold. Host-only routes become map lookups (exact, then `*.` wildcard); every other route runs in
  `order` before them.
- A route targets one pool, a **weighted split** of two to eight pools (weights add up to 100, drawn per request), or a
  redirect. `fallback_pool_ids` adds priority groups: the next pool takes over when every server of the previous one is down.
- `restore_client_ip_header` (for example `CF-Connecting-IP`) restores the real client address behind a CDN; allow lists,
  rate limits and logs then see that address.
- `GET .../virtual-services/{id}/rendered` shows the exact HAProxy configuration, and
  `.../virtual-services/{service}/engines/{engine}/rendered` every file one engine gets.

**Limits.** No OR between conditions (use two routes) and no negation except an absent header. `tcp` services have no routes
except SNI routes on [passthrough ports](tls.md). A weighted draw is per request, not per client: add
[persistence](persistence.md) when a client must stay on one side.

**Decisions:** [0006](../adr/0006-http-routing-semantics.md), [0101](../adr/0101-route-match-model.md),
[0119](../adr/0119-weighted-pool-routes.md). Rendering: [render guide](../render.md).
