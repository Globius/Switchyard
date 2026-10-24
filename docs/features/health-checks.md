# Health checks and drain

**What.** A health monitor (shared between pools) decides whether a member gets traffic. A pool can also judge members by
their live traffic, ramp a recovered member up slowly, and drain members on a timer.

**How to use.**

```yaml
# a monitor
name: http-ok
type: http                   # tcp, http or udp
http: {path: /healthz, expect_status: [200]}   # or expect_body, or expect_header (one of the three)
# or a regex expectation:
#   http: {path: /version, expect_body_regex: '"version":"v[0-9]+[.][0-9]+[.][0-9]+"'}
#   http: {path: /healthz, expect_status_regex: '^2[0-9][0-9]$'}
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

**Decisions:** [0047](../adr/0047-member-health-detail.md), [0066](../adr/0066-pool-passive-checks-and-slow-start.md),
[0108](../adr/0108-health-check-presets.md), [0109](../adr/0109-timed-member-drain.md).
