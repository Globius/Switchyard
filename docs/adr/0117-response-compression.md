# 0117. Response compression

Status: Accepted
Date: 2026-10-12

## Context

Plan item C5: per-service compression. The cache half is decision 6.

## Decisions

1. `VirtualService.compression` (`http` only): `algorithms` (`gzip`, `deflate`; unique, in order of preference) and `content_types` (plain
   lower-case media types; a default list of text-like types). It renders `compression algo ...` and `compression type ...` in the frontend.
2. Media types pass `validate_media_type` and `esc.media_type`: no parameters, no spaces. Images, archives and video are never in the default.
3. Brotli and zstd need libraries the SE image does not ship, so they are not offered.
4. Stored as JSON (`virtual_service.compression`, migration 0028).
5. Stored as JSON: `virtual_service.compression` (migration 0028).
6. `VirtualService.cache` (`http` only): `size_mb` (1 to 256), `max_object_kb` (1 to 8192) and `max_age_s` (1 to 86400). It renders one
   `cache sy_cache_<service>` section (`total-max-size`, `max-object-size`, `max-age`) and, at the end of the request rules, `http-request
   cache-use`, with `http-response cache-store` after the response rules, so deny, rate-limit and redirect rules always run before a cached
   answer can be served. HAProxy stores only cacheable GET/HEAD answers and honours `Vary`; nothing is stored for requests with `Authorization`.
7. Memory quota: the caches of the enabled services of one SE group may add up to 1024 MB (`cache_quota_exceeded`, constant
   `CACHE_MAX_MB_PER_GROUP`), so one tenant cannot take the engines' memory. Storage: `virtual_service.cache` (JSON).
