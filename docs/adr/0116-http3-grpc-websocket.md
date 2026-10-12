# 0116. HTTP/3, gRPC backends and WebSocket timeout

Status: Accepted
Date: 2026-10-12

## Context

Plan item C1: HTTP/3 on ports, and gRPC and WebSocket as explicit options with checks.

## Decisions

1. `VsPort.http3` (needs `tls` and `http2`, `http` services only) adds a QUIC listener on the same number over UDP:
   `bind quic4@<vip>:<port> ... alpn h3` (`quic6@` for IPv6) and an `Alt-Svc` response header so clients upgrade. Verified with `haproxy -c`
   in `haproxy:3.2`. The VIP must accept UDP on that port: the agent's host firewall and the routing layer treat it like any VIP port.
2. `Pool.backend_protocol`: `http1` (default), `h2` (TLS to the members, ALPN `h2`, needs `tls`) or `h2c` (HTTP/2 prior knowledge without
   TLS). gRPC needs one of the HTTP/2 kinds; HAProxy proxies gRPC streams like other HTTP/2 traffic, so there is no separate `grpc` switch.
   `http` pools only (`backend_protocol_needs_http`).
3. WebSocket works through HTTP/1.1 upgrade without options. What operators tune is the idle time, so `Pool.tunnel_timeout_s` renders
   `timeout tunnel` in the backend (HAProxy refuses it on a frontend). It also bounds long gRPC streams.
4. UDP services and pools refuse all of these (`udp_field_not_allowed`). Tables (migration 0030): `vs_port.http3`, `pool.backend_protocol`,
   `pool.tunnel_timeout_s`.
