# Compression and cache

**What.** Response compression and a small in-memory response cache, per `http` service.

**How to use.**

```yaml
compression: {algorithms: [gzip, deflate], content_types: [text/html, application/json]}   # default types are text-like
cache: {size_mb: 64, max_object_kb: 512, max_age_s: 300}
```

The cache is consulted after every deny, rate-limit, WAF and redirect rule, so a cached answer never skips a security rule.
HAProxy stores only cacheable `GET` and `HEAD` answers, honours `Vary`, and stores nothing for requests with `Authorization`.

**Limits.** gzip and deflate only (Brotli and zstd need libraries the engine image does not ship). The caches of one SE
group's enabled services may add up to 1024 MB. Each engine has its own cache.

**Decision:** [0117](../adr/0117-response-compression.md).
