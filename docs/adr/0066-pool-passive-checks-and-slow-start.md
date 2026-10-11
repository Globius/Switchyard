# 0066. Pool passive checks and slow start

Status: Accepted
Date: 2026-10-11

## Context

The 01 feature mapping lists passive health checks (Avi's passive monitor), slow start and external checks. None was in the
model (06 planned-vs-built).

## Decisions

1. `Pool.passive_check` (`observe` `layer4|layer7`, `error_limit` 1 to 1000, `on_error` `mark-down|fastinter|fail-check|sudden-death`)
   renders as `observe <layer> error-limit <n> on-error <action>` on every server line. `Pool.slow_start_s` (1 to 3600)
   renders `slowstart <n>s`. Both are optional and render nothing when unset, so existing output is unchanged.
2. Both only work on top of health checks in HAProxy, so the validator reports `needs_health_monitor` when a pool has either without a
   monitor. `layer7` observation needs HTTP responses: `passive_check_layer7_needs_http` for a pool that a `tcp` service uses.
3. UDP pools reject both (`udp_field_not_allowed`; IPVS has neither), and `GET /schema/virtual-service` lists them as forbidden for UDP.
4. The pool table gets `passive_check` (JSON) and `slow_start_s` (migration 0016).
5. **External checks are not built.** HAProxy's `external-check` runs a command on the SE for every check. That is an arbitrary-command
   path, which the agent must not have (CLAUDE.md, security rules). If it is ever needed it will be a fixed set of named checks shipped
   in the SE image, with its own ADR.
