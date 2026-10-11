# 0047. Per-member health and last check

Status: Accepted
Date: 2026-10-11

## Context

`GET /status/virtual-services` returned only `members_up` and `members_total`, and the tenant summary listed failing
members without saying when or why the check failed (06 planned-vs-built).

## Decisions

1. The HAProxy statistics adapter keeps three more fields per server: `check_status` (`L4OK`, `L7STS`, ...),
   `last_check` (HAProxy's own description of the last check) and `last_change_s` (seconds since the status changed).
   They are stored in the existing `engine_status.members` JSON, so there is no migration. Rows from IPVS (UDP) do not
   have them and omit the keys.
2. Each virtual service in `GET /status/virtual-services` (and the `vs_state` stream event) carries `members`: one row per
   reporting engine and pool member, with pool, member id, address, port, engine and the fields above. Only reachable
   engines of the service's SE group count, as for `health`.
3. `last_checked_at` is the time the controller last read the engine's statistics. HAProxy does not report the absolute
   time of its last check, so this is an upper bound on how old the result is, and `last_change_s` says how long the
   member has been in its current state.
4. Failing members in `GET /summary` carry the same fields.
