# 0225. Per-URL rate-limit key

Status: Accepted
Date: 2026-10-24

## Context

[0110](0110-rate-and-concurrency-limits.md) gave a `rate_limit` rule a `key` of `client_ip` or a
request header. Expressing "no more than N requests per URL" therefore took a path in `match`
plus a client key: the limit was still per client, the request path had to be repeated for every
path worth limiting (against the eight-rule ceiling), and one counter per URL for the whole
service could not be written down at all. The issue asks for a key on the request path, an
explicit normalisation rule, and the two fields that bound the table's memory.

## Decision

**A third key: `key: path`.** One counter per normalised request path. The rule's `match` still
decides which requests are counted, so `match: { path_prefix: /api }` narrows the table to the API
and the key tells apart `/api/orders` from `/api/users`.

**Normalisation.** The key is HAProxy's `path` sample fetch, which is the path as sent:

- the query string is excluded (`?` and everything after it), so `/search?q=a` and `/search?q=b`
  share one counter;
- case is as sent, so `/API` and `/api` are different keys;
- no percent-decoding, no dot-segment resolution and no trailing-slash folding, so `/a/../b`,
  `/a/%2e%2e/b` and `/a/b` are three keys and `/api` and `/api/` are two.

There is no configurable normalisation: a second spelling would make two rules that look alike
count differently.

**Table.** A path rule renders `stick-table type string len 128 ...` and
`http-request track-sc<n> path table <t>` in the same place and slot order as the existing keys.
128 bytes matches the `uri` persistence table. HAProxy truncates a longer path to the declared
length before the lookup, so two paths that agree in their first 128 bytes share a counter; that is
the documented cost of a fixed-size string table, not a silent shortcut.

**Memory bounds, both fields.** `table_size` (100 to 1,000,000 entries, default 10,000) and the new
`entry_ttl_s` (1 to 86,400 seconds, default the larger of `period_s` and 60 s, or 60 s for a
concurrent limit) are validated fields. `entry_ttl_s` may not be shorter than `period_s`: the entry
would expire mid-window and hand the client a fresh allowance. The sum of a service's rule tables
is still capped at `MAX_TABLE_ENTRIES_PER_SERVICE` (2,000,000 entries) with the stable code
`stick_table_too_large`, and the tenant quota `stick_table_entries` still counts the same total.

**Where the key makes no sense.** A path key needs an HTTP service; on any other protocol the
snapshot reports `path_key_needs_http` at `<service>.policy_set_ids`. TCP services cannot carry
policy sets at all, and a UDP service rejects `policy_set_ids` through the field rules of
[0020](0020-udp-m6-implementation.md), so this code is the specific message for the specific
mistake. A rule with `key: header` and no `header_name` is reported on `rules.<n>.header_name`,
the field that is missing, not on the whole rule.

**Counters are per engine, not per group.** Each engine keeps its own stick table; rate-limit
tables are not joined to the group's `peers` section (only persistence tables are, ADR 0118). With
several engines active — VRRP failover or BGP ECMP — a per-path rule of N requests per second is N
per second **per engine**, and a client that reaches two engines gets up to 2N. The same was already
true of client keys; a path key makes it more visible because the counter is shared by every client
of that engine. Table sync for rate limits remains plan item C3.

## Consequences

- Per-URL limits become one rule per policy set instead of one rule per URL, and the counter is
  shared by all clients: use `client_ip` (optionally with a path in `match`) to limit one client
  per URL instead.
- The length limit of 128 bytes and the per-engine semantics are the two surprises an operator can
  hit; both are stated in `docs/features/rate-limits.md`.
- `entry_ttl_s` applies to the older keys too, so a header-key API limit can keep its counters
  longer than the window without a second field per key.
- No new dependency, no free-form config: the key is one more literal and the table type is fixed
  by the renderer.

## Alternatives considered

- **`path,ip` composite (`type string`, `path,src`).** Closer to the old client-keyed behaviour and
  to "one client cannot hammer one URL", but it cannot express a per-URL ceiling for the service,
  which is what the issue asks for; and a composite string key needs a separator that the path may
  contain. Rejected; a path in `match` plus `client_ip` still covers that case.
- **`url` as the key.** Includes the query string, so every cache-buster gets its own counter and
  the table fills with junk.
- **A regex or prefix key (`path_beg` in the key).** Turns the key into an expression language and
  makes the table key unbounded in shape; two rules with overlapping prefixes would count the same
  request twice.
- **One table per path, declared statically.** Multiplies the table count past HAProxy's stick
  counters and needs a separate object per URL.
