# 0006. HTTP routing order, allow lists and drain semantics

Status: Accepted
Date: 2026-10-10

## Context

The design lists `vs_route` as "match (host, path), action (pool, redirect)" and policy sets as
"HTTP rules, allow lists, rate limits", but does not say in what order they are applied, where the
Cloudflare allow list lives, or how a drained member is expressed. HAProxy evaluation order is
visible to users, so it has to be fixed and documented.

## Decision

**Order inside an HTTP frontend**

1. Connection filter: `tcp-request session reject` for sources outside the allow list, when
   `enforce_allow_list` is set.
2. Client address and header hygiene: restore the client address from the configured header only
   when the connection source is in the allow list, delete that header otherwise, always delete
   `X-Forwarded-For`, set `X-Forwarded-Proto`, then `option forwardfor` adds the (restored) source.
3. HTTP to HTTPS redirects (`https_redirect_port`), then policy-set redirect rules in attachment
   order, then redirect routes in `order`.
4. Path-qualified pool routes in `order`.
5. Host-only pool routes: exact host first, then single-label wildcard hosts.
6. `default_backend`, if any. With none, an unmatched request gets HAProxy's 503.

Redirects always run before routing. Path-qualified routes beat host-only routes. Host-only routes
are looked up in map files and are order-independent, so a host appears in exactly one map line.

**Wildcards** follow RFC 6125 everywhere: `*.example.test` matches exactly one label, never the apex
and never `a.b.example.test`. The same rule is used for certificate SAN coverage and for route
matching, and both are tested (the second with real requests).

**Map lookups are guarded** with `-m found`. Without the guard an empty lookup result ends rule
evaluation and later routes are skipped.

**Allow lists are policy sets** of kind `allow_list`, referenced by `allow_list_id` on a virtual
service (decision confirmed with the owner on 2026-10-10). The daily `sync_cloudflare_ips` job will
therefore edit one object (`managed_by: cloudflare`) instead of every virtual service. M1's scope
note said policy sets are "redirect rules only for now"; this adds one small kind.

**Drain and disabled** both render as the `disabled` keyword. HAProxy 3.2 has no `drain` server
keyword in configuration (only the runtime `set server ... state drain`), and a configuration reload
is graceful: connections already established on the old worker finish there, so a drained server
that starts a new worker in maintenance is indistinguishable in practice. The runtime endpoint
(`PUT /pools/{id}/members/{m}/state`) keeps the difference: it sets `drain`, `maint` or `ready` through
the Runtime API without a reload and stores the state, so the next render reproduces it. (An earlier
draft rendered drain as `weight 0`; that would have left a re-enabled server at weight 0.)

**Other defaults chosen**

- A pool member without a port reuses the port the client connected to (`server name ip` with no
  port). A health monitor can set `port` to check a different port; a monitored member with no port
  and no monitor port is a validation error, because HAProxy refuses to start otherwise.
- Backend TLS verification is off unless `tls_verify` is set, matching Avi behaviour for internal
  backends with private certificates; when on, the system CA store is used.
- Persistence is `stick on src` with an IPv6-capable stick table. Syncing tables between the SE pair
  with a `peers` section needs the SE addresses and is added with the agent (M3).
- UDP services are skipped by the HAProxy renderer with a warning; they render to IPVS.
- VIP addresses are bound directly; SEs need `net.ipv4.ip_nonlocal_bind=1` (and the IPv6 equivalent)
  so HAProxy starts on the standby node. That is a property of the SE image, not of the config.

## Consequences

- Behaviour is predictable from the order above and covered by golden files and live-request tests.
- A rule type that does not fit the order (rate limits, WAF) needs a new ADR.
- `https_redirect_port` applies to every host on the port; per-host exceptions would need a new
  field.

## Alternatives considered

- **Route order as the only ordering.** Simple to explain, but host-only routes could not be served
  from map files, and the design asks for SNI/host maps.
- **Allow list inline on each virtual service.** Keeps PolicySet redirect-only but makes the daily
  sync touch every service.
