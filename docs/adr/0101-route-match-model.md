# 0101. Route and rule matches: method, header, query, source network and SNI

Status: Accepted
Date: 2026-10-12

## Context

Plan item A3. A `RouteMatch` held a host and a path. Canary and blue-green routing, API versioning, per-network routes and
tenant-specific names on one VIP need more. Decision 1 of [0100](0100-policy-model-and-request-stages.md) keeps one
condition type for routes and policy rules.

## Decision

`RouteMatch` gains these optional conditions next to `host`, `path_prefix` and `path_exact`. All that are set must hold.

| Field | Meaning | Rendered as |
| --- | --- | --- |
| `methods` | request method is one of up to 7 of GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS | `{ method GET POST }` (sorted) |
| `headers[]` | up to 8 of `{name, op, value}` | `{ req.hdr(Name) -m found }`, `!{ ... -m found }`, `-m str`, `-m beg`, `-m end`, `-m sub` with a double-quoted value |
| `query[]` | up to 8 of `{name, op: present|equals, value}` | `{ url_param(name) -m found }`, `-m str` |
| `source_cidrs[]` | up to 16 networks, IPv4 or IPv6 | `{ src a b c }` (sorted) |
| `sni` | exact name or single-label `*.` wildcard | `{ ssl_fc_sni -m str x }`, `-m reg '<anchored regex>'` |

- `op` for headers is `present`, `absent`, `equals`, `prefix`, `suffix` or `contains`; the last four need a `value`, the
  first two forbid one. Comparisons are case-sensitive on the value and case-insensitive on the name (HAProxy's rule).
- `source_cidrs` sees the client address after client-IP restoration (`restore_client_ip_header`), so behind Cloudflare it
  matches the real client.
- `sni` is never true on a port without TLS. It does not change certificate coverage checks (they look at `host`).
- Header and query values use the header-value validator of [0102](0102-header-and-url-rewrite.md): up to 256 characters
  of letters, digits, spaces and `. _ ~ / : @ + , ; = * ( ) & ! ' -`; no quotes, backslash, `$`, `%` or `#`.
  Header names use the existing header-name validator; query names are letters, digits, `_`, `.` and `-`.
- `is_empty` is true only when no condition is set. A route must still not be empty; rewrite and response rules may be
  (they then apply to every request).

**Routing.** A route that is *only* a host (`is_host_only`) stays a map entry (exact map, then wildcard map). Every other
pool route, whatever its conditions, renders as `use_backend <pool> if <conditions>` and runs in route `order` (ties by
list position) before the host maps. This is the old path-route rule extended: a route with a host and a method is a
conditional route, a route with only a method is allowed. The duplicate-route check compares the whole match.

## Consequences

- Existing routes render byte for byte as before (goldens 01 to 15 unchanged).
- A route with a header or query condition is not cacheable by host, so the host-only fast path matters for services with
  many hosts: keep tenant hosts as host-only routes and use conditional routes for the few exceptions.
- `url_param` reads the query string only (POST bodies are not inspected).
- Stored as `vs_route.match_extra` JSON, so adding a condition later needs no migration of the route table.

## Alternatives considered

- **Regex on host, path or header.** Needed rarely, and every regex is an escaping and ReDoS question. Prefix, suffix and
  contains cover the cases seen so far; add `regex` with its own validator when a case needs it.
- **OR inside one match** (`methods` already is an OR of values). A general OR makes the form and the diff harder to read;
  two routes say the same.
