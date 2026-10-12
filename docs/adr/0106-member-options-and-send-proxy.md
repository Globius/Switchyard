# 0106. Member backup and maxconn, PROXY protocol to members

Status: Accepted
Date: 2026-10-12

## Context

Plan item A6 asks for backup members, per-member connection limits, timed drain and `send-proxy` to backends.

## Decisions

1. `PoolMember.backup` (renders `backup`) and `PoolMember.maxconn` (1 to 1,000,000, renders `maxconn <n>`).
2. `Pool.send_proxy` (`v1` or `v2`) renders `send-proxy` or `send-proxy-v2` on every server, plus `check-send-proxy` when the pool has a
   health monitor, so checks are not rejected by a member that insists on the header.
3. UDP pools reject all three (`udp_field_not_allowed`): IPVS has no equivalent.
4. Tables (migration 0023): `pool_member.backup`, `pool_member.maxconn`, `pool.send_proxy`.
5. **Timed drain is not built.** HAProxy drains by weight and holds sessions until the clients leave. Ending a drain after a time needs a
   controller job that moves the member to `disabled`; it comes with the rolling-upgrade work (plan B4), not with this change.
