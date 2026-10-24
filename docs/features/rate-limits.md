# Rate and concurrency limits

**What.** A `rate_limit` policy set counts requests per client address, per header value (an API key) or per request
path, and denies, tarpits or drops what goes over.

**How to use.**

```yaml
kind: rate_limit
name: api-limits
rules:
  - match: {path_prefix: /api}
    key: client_ip                 # or header, with header_name; or path
    max_requests: 100
    period_s: 10                   # 1, 10, 60, 300 or 3600
    action: deny
    deny_status: 429               # sends Retry-After
  - match: {path_prefix: /upload}
    key: client_ip
    max_concurrent: 4              # requests in flight instead of a rate
    action: tarpit
    tarpit_s: 5
  # one ceiling per URL, shared by every client of this engine
  - match: {path_prefix: /api}
    key: path
    max_requests: 1000
    period_s: 60
    table_size: 20000
    entry_ttl_s: 300               # optional; default is the window, at least 60 s
```

Limits run right after the client address is restored and before redirects, so behind a CDN they count real clients, and
limited traffic never reaches the heavier stages.

**Keys.** `client_ip` (default) counts per client address after client-IP restoration; `header` counts per value of
`header_name` (requests without the header are not counted); `path` keeps one counter per request path.

**What `path` counts.** The path as sent, with the query string ignored: `/search?q=a` and `/search?q=b` share a counter,
while `/API` and `/api` are different keys and `/api/` is not `/api`. There is no case folding, percent-decoding or
dot-segment resolution. HAProxy keeps at most 128 bytes of the path, so paths that agree in their first 128 bytes share a
counter.

**Limits.** Up to 8 rules per service and 2,000,000 table entries per service (also counted against the tenant quota).
`table_size` (100 to 1,000,000) and `entry_ttl_s` (1 to 86,400 seconds) bound a rule's memory; `entry_ttl_s` cannot be
shorter than `period_s`. A `path` key needs an HTTP service; TCP and UDP services cannot carry rate-limit sets at all.

**Counters are per engine, not per group.** Each engine keeps its own stick table, and rate-limit tables are not synced
over `peers` (only persistence tables are). With several engines active (VRRP failover, BGP ECMP) a client can get up to N
times the limit — and for a `path` key, which one counter serves every client, the limit is per engine per path: two
engines mean up to twice the configured rate for that URL across the group.

**Decision:** [0110](../adr/0110-rate-and-concurrency-limits.md), extended by [0225](../adr/0225-rate-limit-path-key.md).
