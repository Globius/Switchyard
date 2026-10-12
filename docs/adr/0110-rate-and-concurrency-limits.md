# 0110. Rate and concurrency limits

Status: Accepted
Date: 2026-10-12

## Context

Plan item A5, plus the login-path part of E10. Tenants need to cap how fast one client may hit a service or a path (login
forms, APIs), how many requests it may have in flight, and to answer the excess in a chosen way. The limits must not become
an expression language (CLAUDE.md), and the stick tables they need use SE memory shared by the tenants of a group.

## Decision

**`kind: rate_limit`.** A policy set with 1 to 8 `rules`. Each rule:

| Field | Meaning |
| --- | --- |
| `match` | which requests are counted and limited ([0101](0101-route-match-model.md)); empty means all; a path in the match is a per-path limit |
| `key` | `client_ip` (default, after client-IP restoration) or `header` with `header_name` (an API key header). Requests without the header are not counted |
| `max_requests` and `period_s` | allowed requests per window of 1, 10, 60, 300 or 3600 seconds |
| `max_concurrent` | allowed requests in flight (exactly one of the two limits) |
| `action` | `deny` (status `deny_status`: 403, 429 or 503; with `Retry-After: <period_s>` for rate limits), `tarpit` (hold `tarpit_s` seconds, then 429), `silent_drop` |
| `table_size` | keys the rule's table holds (100 to 1,000,000, default 10,000) |

**Stage.** Rate limits run right after header hygiene and before redirects, so the client address is the restored one and
limited traffic never reaches the heavier stages (stage 2b in [0100](0100-policy-model-and-request-stages.md), whose table
gets this row).

**Rendering.** Per rule: a dummy `backend rl_<service>_<set>_<n>` that holds only the stick table
(`type ipv6` for addresses, `type string len 64` for header keys; `store http_req_rate(<period>s)` or `conn_cur`; expiry is
the larger of the period and 60 s), then `http-request track-sc<slot> <key> table <t> [if <match>]` and the enforcing line
(`return status ... if { sc_http_req_rate(<slot>,<t>) gt <max> }`, `tarpit deny_status 429`, or `silent-drop`). The slot is
the rule's position among the service's rate-limit rules, so rules never share a counter; the global section gets
`tune.stick-counters <n>` when a service needs more than HAProxy's default of 3. `timeout tarpit` is set per frontend.

**Ceilings.**
- At most 8 rules per service (`too_many_rate_limits`) and 2,000,000 table entries per service (`stick_table_too_large`).
  All tarpit rules of a service share one `tarpit_s` (`tarpit_conflict`), because HAProxy has one tarpit timeout per frontend.
- Tenant quota `stick_table_entries` (a quota key next to the object type names): the total `table_size` of the tenant's
  rules, counted per rule per service that uses the set. Like other quotas it only blocks changes that grow the number
  (ADR 0045) and is reported as `quota_exceeded` at `quota.stick_table_entries`.

**Login-path limits (E10 part)** are a rule with `match.path_exact` or `path_prefix` plus `methods: [POST]`; no separate object.

## Consequences

- Limits are per engine: with several engines in a group (VRRP or BGP/ECMP) each keeps its own counters, so a client's
  effective limit is up to N times the value. Table sync with `peers` is plan item C3.
- A limit applies per client address; clients behind one NAT share it. Header keys avoid that for APIs.
- Memory: an entry costs roughly 100 bytes; the default quota is unset, so operators should set `stick_table_entries` for
  tenants they do not trust.
- `conn_cur` counts requests in flight (streams), not TCP connections.
- Services without these sets render as before.

## Alternatives considered

- **One shared table per service with several counters (`gpc`, `http_req_rate` of different periods).** Fewer backends, but
  rules would share a key and an expiry; separate tables keep the model simple.
- **Using `src_http_req_rate` without tracking.** It needs a counter updated by someone; `track-sc` is the supported way.
- **Quotas per object count only.** Table memory, not object count, is what the SE pays for.
