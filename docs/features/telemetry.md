# Metrics and logs

**What.** Engines expose HAProxy and node metrics on their management address; Vector on each engine ships HAProxy, WAF and
login logs to Loki. The controller reads Prometheus and Loki with tenant-scoped queries, so a tenant sees only its own services,
and proxies Grafana with the same scoping.

**How to use.**

- Run `deploy/telemetry/` (Prometheus, Loki, Grafana), point it at the controller, and set `SWITCHYARD_PROMETHEUS_URL` and
  `SWITCHYARD_LOKI_URL` on the controller: [Telemetry runbook](../runbooks/telemetry.md).
- Per service: `GET .../virtual-services/{id}/metrics` and `.../logs`; for the tenant: `GET .../logs`,
  `GET .../metrics/virtual-services`; per engine: `GET .../engines/{engine}/metrics`.
- The controller's own metrics are at `GET /metrics`, and Prometheus finds engines through `GET /api/v1/discovery/prometheus`
  (system administrators or `SWITCHYARD_METRICS_TOKEN`).

**Limits.** Without Prometheus and Loki the metrics and log endpoints answer `available: false` and the UI hides the panels.
Engine metrics need a management network on the group. No Alertmanager is deployed by default.

**Decisions:** [0052](../adr/0052-telemetry-pipeline.md), [0061](../adr/0061-controller-metrics-and-sd.md),
[0063](../adr/0063-haproxy-exporter-listener.md), [0085](../adr/0085-grafana-behind-controller.md),
[0123](../adr/0123-tenant-log-api.md).
