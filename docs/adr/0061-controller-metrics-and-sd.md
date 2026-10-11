# 0061. Controller metrics and Prometheus service discovery

Status: Accepted
Date: 2026-10-11

## Context

02 (telemetry) promises controller metrics and an HTTP service-discovery endpoint so new SEs appear in Prometheus with
`tenant`, `se_group` and `vs` labels. Neither existed (06 planned-vs-built).

## Decisions

1. `GET /metrics` renders Prometheus text format 0.0.4 with a small renderer in `services/observability.py`; no
   `prometheus_client` dependency. Metrics are computed from the database on each scrape (job queue, finished job
   run time as a summary, change sets by state, engine reachability and drift, age of the newest poll), so they are the
   same on every replica and survive restarts. Process-local counters would be lost or differ per replica.
2. `GET /api/v1/discovery/prometheus` returns `http_sd_configs` groups for enabled engines that have a management
   address: `tenant` (id, the label the telemetry contract already uses), `tenant_name`, `se_group`, `engine`, `vs`.
   `vs` holds the group's service names between commas so one relabel regex can match a service.
3. Ports are conventions, overridable per request: HAProxy exporter 8405 (8404 stays the local health endpoint) and node
   exporter 9100. Serving them is the SE image's job (operations thread); the renderer is unchanged.
4. Access: a system administrator, or the bearer token in `SWITCHYARD_METRICS_TOKEN`, compared in constant time. That token
   opens only these two routes, so Prometheus does not need an administrator account. Both routes are
   cross-tenant and so are never available to tenant roles.
