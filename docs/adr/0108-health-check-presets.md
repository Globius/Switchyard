# 0108. Health checks: body and header match, protocol presets

Status: Accepted
Date: 2026-10-12

## Context

Plan item A8: HTTPS body and header matches, and protocol-aware checks for Redis, SMTP, LDAP and MySQL. ADR 0066 keeps arbitrary commands out.

## Decisions

1. `HttpCheck.expect_body` (`http-check expect string`) and `HttpCheck.expect_header` (`name`, optional `value`; `http-check expect hdr name ...`).
   Exactly one of `expect_status`, `expect_body` and `expect_header` may be set, because one `http-check expect` line takes one matcher.
   Match text is restricted to letters, digits and `._:/-` (`validate_match_text`, re-checked by `esc.match_text`). HTTPS checks are a pool
   with `tls`, as before.
2. `HealthMonitor.preset` on a `tcp` monitor: `redis`, `smtp`, `ldap` or `mysql`, rendered with HAProxy's built-in `option redis-check`,
   `option smtpchk`, `option ldap-check` and `option mysql-check`. A fixed list: no custom send and expect strings.
3. A `preset` on a non-`tcp` monitor is refused by the model; a `tcp` monitor on a UDP pool is refused as before.
4. Table (migration 0028): `health_monitor.preset`. The HTTP additions live in the existing `http` JSON column.
