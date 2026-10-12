# Health checks and drain

**What.** A health monitor (shared between pools) decides whether a member gets traffic. A pool can also judge members by
their live traffic, ramp a recovered member up slowly, and drain members on a timer.

**How to use.**

```yaml
# a monitor
name: http-ok
type: http                   # tcp, http or udp
http: {path: /healthz, expect_status: [200]}   # or expect_body, or expect_header (one of the three)
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
- Draining a member that has `drain_timeout_s` disables it once the time has passed (the controller's `expire_drains` job,
  every 30 seconds). Without it a drained member stays drained until someone acts.
- `GET .../status/virtual-services` reports each member's state and its last check result.

**Limits.** HTTPS checks are a pool with `tls`. Passive checks and slow start need a health monitor; `layer7` observation
needs HTTP. **No external or scripted checks:** they would run commands on engines, which the agent must never do.

**Decisions:** [0047](../adr/0047-member-health-detail.md), [0066](../adr/0066-pool-passive-checks-and-slow-start.md),
[0108](../adr/0108-health-check-presets.md), [0109](../adr/0109-timed-member-drain.md).
