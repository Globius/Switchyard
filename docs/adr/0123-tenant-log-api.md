# 0123. Tenant-wide request log API

Status: Accepted
Date: 2026-10-12

## Context
The per-service access log (A9) answers "what happened to this service". Operators also ask "what is failing across my
tenant right now", and the alerting work needs a place to send people to look.

## Decision
`GET /tenants/{t}/logs` returns HAProxy request lines for every virtual service of the tenant, newest first, with the same
filters, paging and `available: false` behavior as the per-service log, plus an optional `service` name. It reuses the
telemetry client: the LogQL selector is `{tenant="<id>"}` (plus `frontend="vs_<name>"` for one service), built in the
adapter with the service name validated as a safe identifier, and the same selector rewriting that protects Grafana
(ADR 0085) covers anything that reaches Loki through Grafana. Any role in the tenant may read.

## Consequences
One query spans all services, so Loki cost grows with traffic; the one-hour default and the 500-line limit bound it.
Live tail, saved searches and log-based alert rules are not part of this decision.

## Alternatives considered
Extending the per-service endpoint with a wildcard (changes an existing contract); exposing Loki's own API (cannot enforce
the tenant label without the proxy of ADR 0085).
