# 0052. Telemetry pipeline: Prometheus, Loki, Grafana, Vector

Status: Accepted
Date: 2026-10-11

## Context
06 lists the metrics and logs pipeline as not built: the API answers `available: false` because no
Prometheus or Loki exists. The controller already exposes `/metrics` and an HTTP service-discovery list
(ADR 0061) and renders the HAProxy exporter listener (ADR 0063); the SE image has the node exporter.

## Decision
- `deploy/telemetry/compose.yaml` runs Prometheus, Loki and Grafana on one VM. Prometheus scrapes the
  controller VIP and the two discovery lists (`exporter=haproxy`, `exporter=node`), authenticating with
  `SWITCHYARD_METRICS_TOKEN` read from a file. The VIP address and scheme are the only settings.
- Logs: Vector on every SE reads HAProxy's journald output, extracts the frontend, and pushes to Loki with
  the labels the controller queries (`tenant` = tenant id, `frontend`), plus `tenant_name`, `se_group` and
  `engine` for dashboards. The per-SE values come from `/etc/switchyard/vector.env` in the cloud-init
  snippet; Vector stays dormant until that file exists.
- Grafana is provisioned with the two data sources and three dashboards (controller, per-SE HAProxy,
  nodes). Alert rules for a stale status poll, failed change sets and a down exporter are in
  `prometheus/rules.yml`; no Alertmanager is deployed yet.
- Setting `SWITCHYARD_PROMETHEUS_URL` and `SWITCHYARD_LOKI_URL` on the controller turns the UI panels on.

## Consequences
Single instances with local disks, 30 days of metrics and 14 days of logs: enough for one site, not HA. The
controller's tenant-scoped queries still depend on the `tenant` label being correct; a mis-labelled SE would
show another tenant's data, so `vector.env` is written by the deploy job, never by hand. Vector 0.59 refuses
to read environment variables in its config unless
`VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION=true` is set, which the unit does (the values are not secret).
The haproxy and node dashboards were checked for valid provisioning only; their queries need a real SE to
show data.

## Alternatives considered
VictoriaMetrics (smaller, but a second PromQL dialect to test); Promtail (deprecated in favor of Alloy);
running the stack inside the controller Compose (couples its restarts to the HA database nodes).
