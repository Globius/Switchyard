# Health checks and drain

**What.** A health monitor (shared between pools) decides whether a member gets traffic. A pool can also judge members by
their live traffic, ramp a recovered member up slowly, and drain members on a timer.

**How to use.**

```yaml
# a monitor
name: http-ok
type: http                   # tcp, http, grpc or udp
http: {path: /healthz, expect_status: [200]}   # or expect_body, or expect_header (one of the three)
# or a regex expectation:
#   http: {path: /version, expect_body_regex: '"version":"v[0-9]+[.][0-9]+[.][0-9]+"'}
#   http: {path: /healthz, expect_status_regex: '^2[0-9][0-9]$'}
---
name: grpc-health
type: grpc                   # only on a pool whose backend_protocol is h2 or h2c
grpc: {service: acme.echo.Health, expect_status: [200]}
---
name: redis
type: tcp
preset: redis                # redis, smtp, ldap or mysql: HAProxy's built-in protocol checks
---
# on a pool
passive_check: {observe: layer7, error_limit: 10, on_error: mark-down}
slow_start_s: 60
members:
  - {name: web-1, address: 10.0.1.21, port: 80, drain_timeout_s: 600}
```

- `POST .../health-monitors/{monitor_id}/test` runs the check once against a target, from the controller.
- `expect_body_regex` and `expect_status_regex` are the opt-in regex forms of `expect_body` and `expect_status`
  (`http-check expect rstring` / `rstatus`). Reach for them when the literal matchers cannot express the
  expectation: a version string (`^v[0-9]+[.][0-9]+$`) or a JSON fragment (`"status":"ok"`) rather than a fixed
  token. Exactly one expectation may be set, regex or literal; the literal fields keep their old, narrower
  character set so existing monitors render exactly as before.
- A regex is validated when the change set is built, not when the config is rendered: it must be 1-256
  characters, must not carry a log-format variable (`%[...]`, `%{...}`), must not contain a single quote or a
  control character, and must compile. A pattern that fails is a field error with code `invalid_regex` on the
  offending `http` field, so a bad regex never reaches an engine. **ReDoS:** HAProxy compiles and evaluates the
  pattern on every check against every member, so a pathological pattern costs CPU on the SE continuously. The
  length cap and the compile check are the mitigation: they keep the pattern small and syntactically sane.
  Patterns are user-supplied, so keep them anchored and simple.
- Draining a member that has `drain_timeout_s` disables it once the time has passed (the controller's `expire_drains` job,
  every 30 seconds). Without it a drained member stays drained until someone acts.
- `GET .../status/virtual-services` reports each member's state and its last check result.

**Limits.** HTTPS checks are a pool with `tls`. Passive checks and slow start need a health monitor; `layer7` observation
needs HTTP. **No external or scripted checks:** they would run commands on engines, which the agent must never do.

## gRPC and HTTP/2 pools

A `grpc` monitor checks a gRPC server the way HAProxy can: an HTTP/2 request to the standard health method, not a gRPC
stream. It renders, next to the pool's other check lines:

```text
option httpchk
http-check connect default proto h2
http-check send meth POST uri /grpc.health.v1.Health/Check ver HTTP/2 hdr content-type application/grpc hdr te trailers
http-check expect status 200 comment service=acme.echo.Health
```

- `grpc: {service: <name>, expect_status: [200]}`. `service` is a gRPC service name (`[A-Za-z0-9_.-]`, at most 128
  characters; empty means overall server health) and `expect_status` the HTTP codes that mean healthy (100-599, at
  least one). Interval, timeout, rise and fall are the monitor's own fields, as for every other kind.
- The pool must speak HTTP/2: `backend_protocol: h2` (with `tls`) or `h2c`. On any other pool the change is refused
  with the code `grpc_check_needs_h2` on `pools.<name>.health_monitor_id`, because an h1 exchange cannot carry a gRPC
  request and would mark a working server down.
- `connect default proto h2` keeps the server line's options (`check-ssl`, `check-sni`, `alpn h2`) and forces the
  HTTP/2 multiplexer, so the check follows the same TLS and server-name settings as the pool's traffic (ADR 0222).
- A failing check marks the member down in the usual member rows (`GET .../status/virtual-services`, HAProxy's
  `L7STS`/`L4CON` in `last_check`); the failure text carries `service=<name>`, so an operator can see which service the
  check was for.

**Limits of the gRPC check.** HAProxy 3.2 has no `option grpc-check`, and it cannot send a gRPC request frame from a
health check (config strings are NUL-terminated and a frame starts with a 0x00 byte) or read `grpc-status` from the
response trailers. The probe therefore proves that the port speaks HTTP/2 and dispatches the health handler, on the
HTTP status that ended the exchange. **It does not inspect per-service status:** a server that answers `NOT_SERVING`
(still an HTTP 200 with `grpc-status: 2`) passes, and the `service` name labels the check rather than selecting a
service. Health checks run **per engine**, on each SE, like every other check; nothing is aggregated by the controller.
If you need a real SERVING verdict, add an HTTP/1.1 health endpoint next to the gRPC port and use an `http` monitor,
or check from outside the data plane. Running `grpc_health_probe` from the engine is not an option: the agent never
runs commands on an SE.

**Decisions:** [0047](../adr/0047-member-health-detail.md), [0066](../adr/0066-pool-passive-checks-and-slow-start.md),
[0108](../adr/0108-health-check-presets.md), [0109](../adr/0109-timed-member-drain.md),
[0227](../adr/0227-grpc-health-check.md).
