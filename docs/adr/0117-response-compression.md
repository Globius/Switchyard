# 0117. Response compression

Status: Accepted
Date: 2026-10-12

## Context

Plan item C5: per-service compression. The cache half of C5 is not part of this decision.

## Decisions

1. `VirtualService.compression` (`http` only): `algorithms` (`gzip`, `deflate`; unique, in order of preference) and `content_types` (plain
   lower-case media types; a default list of text-like types). It renders `compression algo ...` and `compression type ...` in the frontend.
2. Media types pass `validate_media_type` and `esc.media_type`: no parameters, no spaces. Images, archives and video are never in the default.
3. Brotli and zstd need libraries the SE image does not ship, so they are not offered.
4. Stored as JSON (`virtual_service.compression`, migration 0029).
5. Not built: the HAProxy small-object cache (`cache`), which needs a memory quota per tenant first.
