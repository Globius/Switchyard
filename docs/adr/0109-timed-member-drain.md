# 0109. Timed member drain

Status: Accepted
Date: 2026-10-12

## Context

HAProxy drains by weight and keeps existing sessions until clients leave. Plan item A6 wants a drain that ends: after a time the member goes
out of service.

## Decisions

1. `PoolMember.drain_timeout_s` (1 to 604,800, default none). With none, a drained member stays drained until an operator acts, as before.
2. The controller owns the timer; the engine never learns about it. `pool_member.drain_since` records when the controller saw the member start
   draining. It is runtime state (not in the snapshot or the change set), set by `MemberService.set_state` when the state becomes `drain` and
   cleared by any other state.
3. A scheduled job `expire_drains` (every 30 seconds, one leader, like `poll_status`) moves each member whose `drain_since + drain_timeout_s`
   has passed to `disabled`, through the same code as an operator (stored, version bump, audit event `pool_member.state` by
   `system:drain-timeout`, Runtime API on every engine). A member found draining with a timeout but no `drain_since` (for example a change set
   wrote the row) gets its clock started, so the timeout counts from the first time the job sees it.
4. Timing is therefore accurate to the job interval, and a controller outage delays but never skips the disable. Engines keep serving.
5. Table: `pool_member.drain_timeout_s`, `pool_member.drain_since` (migration 0028).
