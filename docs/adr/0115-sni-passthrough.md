# 0115. TLS passthrough with SNI routing on TCP services

Status: Accepted
Date: 2026-10-12

## Context

Plan item C4: route TLS to different pools by the server name without terminating TLS (no certificate on the SE).

## Decisions

1. `VsPort.tls_passthrough` (`tcp` services only) excludes `tls`, `http2`, `http3`, `tls_profile`, `client_ca_id` and `https_redirect_port`.
2. A `tcp` service whose ports are all passthrough ports may have `routes`. A route matches a host only (exact or `*.` wildcard, same rules as
   HTTP routes) and names a pool; paths and redirects are refused with `passthrough_route_invalid`. Routes on any other `tcp` service are still
   refused. `default_pool_id` stays required and catches clients with no SNI or an unknown name.
3. The frontend waits up to 5 seconds for the client hello (`tcp-request inspect-delay`), accepts only a TLS hello, and chooses the backend from
   `req.ssl_sni` lower-cased through map files, exact first, then wildcards. The map keys pass the hostname validator and `esc.hostname`.
4. The existing `routes` table and its pool-reference checks are reused, so no new table. Migration 0029 adds `vs_port.tls_passthrough`.
5. Because TLS stays end to end, HTTP features (headers, redirects, compression) do not apply. `proxy_protocol` on the port (accept) still can.
