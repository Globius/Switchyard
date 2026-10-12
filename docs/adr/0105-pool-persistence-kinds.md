# 0105. Pool persistence: cookie, header and URI

Status: Accepted
Date: 2026-10-12

## Context

Pools only had source IP persistence. Avi parity (08 plan, A1) needs cookie persistence and persistence on a header or the URI.

## Decisions

1. `Pool.persistence` becomes a discriminated union on `type`: `source_ip` (unchanged, and still the default shape), `http_cookie`,
   `header` and `uri`. Stored in the same JSON column, so there is no migration; old rows load unchanged.
2. `http_cookie`: `name`, `mode` (`insert`, `rewrite`, `prefix`), and for `insert` only `ttl_s` (`maxlife`), `secure` and `http_only`
   (default on). It renders `cookie <name> <mode>` (`insert` adds `indirect nocache`) and `cookie <server name>` on each server, so the cookie
   value is the stable server name, never an address.
3. `header` (`header`) and `uri` (`source` `path` or `url_param`, with `param` for the latter) use a `type string len 128` stick table and
   `stick on req.hdr(...)`, `path` or `url_param(...)`. Requests without the key fall back to the algorithm.
4. Cookie, header and URI persistence need HTTP: `persistence_needs_http` for a pool a `tcp` service uses; `udp_field_not_allowed` for UDP
   (IPVS persistence stays the `source` algorithm). Names pass strict validators (`validate_cookie_name`, header and name validators) and the
   renderer checks them again (`esc.cookie_name`).
5. Sharing the table between SEs (`peers`, plan C3) is not part of this.
