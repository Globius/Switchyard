<!--
SPDX-FileCopyrightText: 2026 The Switchyard contributors

SPDX-License-Identifier: Apache-2.0
-->

# 0227. gRPC health checks: an HTTP/2 request, not a gRPC stream

Status: Accepted
Date: 2026-10-24

## Context

`MonitorType` was `tcp`, `http` and `udp`. A gRPC pool was only recognisable by its
`backend_protocol` (`h2`/`h2c`, ADR 0116), and its members were checked with a bare TCP connect or
an HTTP/1.1 request. A pool of gRPC servers whose health service reports `NOT_SERVING`, or which
answers HTTP/1.1 with an error page, therefore stayed healthy while every stream failed.

The obvious directive would be `option grpc-check`. **It does not exist in HAProxy 3.2.** The 3.2
configuration manual's alphabetical keyword index and its health-check sections list `option
httpchk`, `option tcp-check`, `option mysql-check`, `option pgsql-check`, `option redis-check`,
`option ldap-check`, `option smtpchk` and `option spop-check`, and no `option grpc-check`
(<https://docs.haproxy.org/3.2/configuration.html>, checked 2026-10-24). The only gRPC-aware
feature in the manual is the `ungrpc` sample convertor, which reads a gRPC message from a request
body on the data path, not on a health check.

What HAProxy 3.2 *can* do is run a health check over HTTP/2: `http-check connect` takes a `proto
<name>` argument that "forces the multiplexer's protocol" (it "must be an HTTP mux protocol"), and
`http-check send` takes `ver HTTP/2`. HAProxy's own health-check tutorial uses exactly that to
check HTTP/2 servers:

```text
option httpchk
http-check connect ssl alpn h2
http-check send meth HEAD uri /health ver HTTP/2 hdr Host www.test.local
```

(`https://www.haproxy.com/documentation/haproxy-configuration-tutorials/reliability/health-checks/`,
checked 2026-10-24.)

## Decision

A new monitor type, `grpc`, renders an HTTP/2 `http-check` ruleset in the pool's backend, next to
the existing check lines:

```text
option httpchk
http-check connect default proto h2
http-check send meth POST uri /grpc.health.v1.Health/Check ver HTTP/2 hdr content-type application/grpc hdr te trailers
http-check expect status 200 comment service=acme.echo.Health
```

1. `connect default proto h2` keeps the server line's options (`check-ssl`, `check-sni <name>`,
   `alpn h2`) and forces the HTTP/2 multiplexer, which is what makes the check speak h2/h2c
   instead of HTTP/1.1. The `server` line already carries `proto h2` for `backend_protocol: h2c`
   and `alpn h2` for `h2`.
2. The request is `POST /grpc.health.v1.Health/Check` with `content-type: application/grpc` and
   `te: trailers`, the two headers a gRPC server needs before it dispatches the health handler.
3. The member is accepted on the HTTP status the exchange ended with (`expect_status`, 200 by
   default). The `comment` carries the service name, so the name appears in the failure text
   HAProxy reports for the member and the UI shows as its last check (ADR 0047).

`GrpcCheck` holds `service` (`[A-Za-z0-9_.-]`, at most 128 characters; empty means overall server
health) and `expect_status` (at least one code, 100-599). The interval is the monitor's existing
`interval_s` (1-300 s, rendered as `default-server inter`), so a gRPC monitor has the same one
interval, rise and fall as every other monitor. A `grpc` monitor without a `grpc` block, and a
`grpc` block on any other type, are model errors.

The validator rejects a `grpc` monitor on a pool whose `backend_protocol` is not `h2` or `h2c`:
code `grpc_check_needs_h2` on `pools.<name>.health_monitor_id`. `GET /api/v1/schema/virtual-service`
offers `grpc` for `http` services only, since only their pools may speak HTTP/2.

## Consequences

- A dead, restarted or non-gRPC listener on a member is now caught: the connect fails, or the
  HTTP/2 exchange fails, and the member goes down after `fall` consecutive failures. This is the
  failure the issue described.
- **The check is not a gRPC health *evaluation*.** HAProxy cannot put a gRPC request frame on the
  wire from a health check: the frame starts with a 0x00 compression flag and config string
  arguments are NUL-terminated (`tcp-check send-binary` exists precisely because binary cannot be
  written as a string), and it cannot read `grpc-status` from the response trailers. So the probe
  proves "this port speaks HTTP/2 and dispatches the health method", not "this service reports
  SERVING". A server that answers `NOT_SERVING` (still an HTTP 200 with `grpc-status: 2`) passes.
  This limit is documented in `docs/features/health-checks.md` and must be quoted whenever the
  feature is described: **no per-service status inspection.**
- Health checks run per engine, on each SE, as every other check does. Nothing about the result is
  aggregated by the controller.
- The service name is metadata the engine cannot transmit, so it is a label on the check, not a
  filter. Operators who need a real SERVING check must expose an HTTP/1.1 health endpoint
  (`http` monitor) or check from outside the data plane; running `grpc_health_probe` from the
  engine was rejected (see below).
- Migration 0050 adds `health_monitor.grpc` (JSONB) and widens the `type` check constraint. The API,
  the OpenAPI document, `ui/src/api/schema.d.ts` and the OpenTofu provider are regenerated.

## Alternatives considered

1. **`option grpc-check`.** Does not exist in HAProxy 3.2, as cited above. Rejected as impossible.
2. **`http-check send` with the gRPC health request as its `body`.** The only way to a true
   per-service answer (`grpc-status` in the trailers) would be to send a framed `HealthCheckRequest`
   and match the response. The 5-byte frame prefix starts with a 0x00 byte, which a config string
   cannot carry, so the body would be truncated to empty. Rejected as unbuildable.
3. **`http-check expect hdr name grpc-status value 0`.** gRPC puts the status in trailers; whether
   the check engine exposes h2 trailers as response headers is not documented, and the failure mode
   is total (every member of every gRPC pool marked down). Rejected as unverifiable; revisit only
   with a container test that proves it.
4. **`external-check` with `grpc_health_probe`.** A correct gRPC check, but it runs a process on the
   SE, and the agent must never run commands on an engine; ADR 0108 and the health-check docs
   already forbid scripted checks. Rejected.
5. **Inferring the check from `backend_protocol: h2`/`h2c` alone** (the status quo). It cannot be
   turned off or tuned per pool, and it hides the fact that the probe is not a gRPC one. Rejected in
   favour of an explicit monitor type the operator chooses.
6. **A `grpc` block on an `http` monitor instead of a new type.** Fewer changes, but the rendered
   lines and the `h2` requirement differ enough that a form would have to special-case the type
   anyway; a distinct type keeps the API honest about what the engine will do.
