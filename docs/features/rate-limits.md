# Rate and concurrency limits

**What.** A `rate_limit` policy set counts requests per client address or per header value (an API key) and denies, tarpits
or drops what goes over.

**How to use.**

```yaml
kind: rate_limit
name: api-limits
rules:
  - match: {path_prefix: /api}
    key: client_ip                 # or header, with header_name
    max_requests: 100
    period_s: 10                   # 1, 10, 60, 300 or 3600
    action: deny
    deny_status: 429               # sends Retry-After
  - match: {path_prefix: /upload}
    key: client_ip
    max_concurrent: 4              # requests in flight instead of a rate
    action: tarpit
    tarpit_s: 5
```

Limits run right after the client address is restored and before redirects, so behind a CDN they count real clients, and
limited traffic never reaches the heavier stages.

**Limits.** Up to 8 rules per service and 2,000,000 table entries per service (also counted against the tenant quota).
Counters are per engine: with several engines active (BGP ECMP) a client can get up to N times the limit. Requests without
the key header are not counted.

**Decision:** [0110](../adr/0110-rate-and-concurrency-limits.md).
