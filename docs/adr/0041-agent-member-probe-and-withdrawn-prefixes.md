# 0041. Agent member probe and the list of withdrawn prefixes

Status: Accepted
Date: 2026-10-10

## Context

The UI needs "test this health monitor against one member" and, in BGP mode, which prefixes the
watchdog has withdrawn (ADR 0020 withdraws per prefix, the status had one boolean).

## Decisions

1. **`Agent.ProbeMember`** runs one tcp, http or udp probe from the engine. It is not a general
   network tool: the target `address:port` must already appear as a `server` of the applied
   `haproxy.cfg` (including its `port` check override) or a `real_server` of the applied
   `keepalived.conf`; otherwise the agent answers `refused` without connecting. HTTP is limited to
   GET, HEAD and OPTIONS, a strict path and host, no redirects; timeouts are 100 ms to 10 s; the reply
   body is never returned, only a short detail such as `HTTP 200`.
2. **Controller route** `POST /tenants/{t}/health-monitors/{id}/test` (operator) picks the first enabled
   engine with an agent in a group that serves the pool (or the given `engine_id`), builds the probe
   from the monitor (overrides: port, path, expected codes, timeout), saves nothing and answers
   `ok`, `latency_ms`, `detail`, `engine`, `target`. A member that is only in a draft is refused
   (`409`: apply first); an unreachable agent is `502`.
3. **`Status.withdrawn_prefixes`** (sorted) is added next to `routes_withdrawn`, which stays as "any".
   The API exposes both under `agent`.

## Consequences

- Agents must be upgraded before the route works; older agents answer `UNIMPLEMENTED` (reported as `502`).
- A UDP test needs the member to reply; Keepalived's silent-is-healthy semantics are not reproduced.
- The probe sees the network from the engine, which is the point; it cannot reach arbitrary hosts.

## Alternatives considered

- Probing from the controller: wrong vantage point and it would need network access to every backend.
- A free-form target on the RPC: a port scanner on every SE; rejected by the security rules.
