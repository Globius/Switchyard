# Telemetry stack (Prometheus, Loki, Grafana)

Setup: `deploy/telemetry/`. Smoke test: `sh deploy/telemetry/smoke.sh` (needs the dev PostgreSQL and a built controller
image; checks the scrape, discovery, Loki and Grafana provisioning).

**Install.** Copy `telemetry.env.example` to `telemetry.env`, put the value of `SWITCHYARD_METRICS_TOKEN` in
`secrets/metrics.token`, then `docker compose --env-file telemetry.env up -d`. On the controller set
`SWITCHYARD_PROMETHEUS_URL=http://<host>:9090` and `SWITCHYARD_LOKI_URL=http://<host>:3100` and restart the API;
the UI charts and log tail stop saying "no data source".

**Beside the quickstart.** `docker compose -f compose.yaml -f compose.quickstart.yaml --env-file telemetry.env up -d` (the
override joins Prometheus to the quickstart's network; use `CONTROLLER_SCHEME=http`, `CONTROLLER_HOSTPORT=api:8000`).
The token file and `scrape.d/pve.yml` must be readable by the Prometheus user (mode 644), or Prometheus restarts in a loop
with "open ... permission denied". The scrape works against IPv6 management addresses too (the discovery endpoint
returns bracketed targets), provided Prometheus can route to them: with the quickstart's `--ipv6` network it does.

**Checks when something looks empty.**
1. Prometheus `Status > Targets`: `switchyard`, `se-haproxy`, `se-node` should be up. An empty `se-*` job means
   no enabled engine has a management address, or the token is wrong (`401` in the target error).
2. `se-haproxy` down but `se-node` up: the SE group has no `mgmt` network, so no exporter listener is rendered
   (ADR 0063), or the scrape comes from outside that network.
3. Logs missing: on the SE `systemctl status vector` (it only runs once `/etc/switchyard/vector.env` exists, which cloud-init
   writes when snippets are used; for engines cloned through the API run `enroll-engine.sh --vector-env FILE` with
   `LOKI_URL`, `TENANT_ID`, `TENANT_NAME`, `SE_GROUP` and `ENGINE`); `journalctl -u vector`. Vector starts with Loki not yet
   ready (503 in the health check) and recovers on the unit's restart. In Grafana Explore, `{tenant_name="…"}` should show lines.
4. UI shows another tenant's or no data: compare the `tenant` label on the SE's series with the tenant id.

**Engine log sinks: Loki and OTLP ([adr/0221](../adr/0221-optional-otlp-log-sink.md)).** Vector on the engine ships HAProxy,
Coraza and sy-auth logs to every sink whose URL is set in `/etc/switchyard/vector.env`; `sy-vector-sinks` writes the sinks
at each start (`/run/switchyard-vector/sinks.yaml`, mode 0600).

| Key | Meaning |
| --- | --- |
| `TENANT_ID`, `TENANT_NAME`, `SE_GROUP`, `ENGINE` | Required. Labels on Loki, resource attributes on OTLP |
| `LOKI_URL` | Loki base URL. Unset: no Loki sink (the controller's log tail and p95 then see nothing from this engine) |
| `OTLP_LOGS_URL` | OTLP/HTTP collector base URL, for example `https://collector.example.org:4318`; logs go to `/v1/logs`. Unset: no OTLP sink |
| `OTLP_LOGS_HEADERS` | Optional. `Name: value` pairs joined by `;` (`Authorization: Bearer <token>`). Never logged |
| `OTLP_LOGS_INSECURE_SKIP_VERIFY` | Optional, default `false`. `true` skips TLS verification (labs only) |
| `OTLP_LOGS_CA_FILE` | Optional. PEM bundle trusted for the collector, a path on the engine |

```sh
# /etc/switchyard/vector.env: Loki and OTLP
TENANT_ID=t-acme
TENANT_NAME=acme
SE_GROUP=edge
ENGINE=engine-1
LOKI_URL=http://loki.example.org:3100
OTLP_LOGS_URL=https://collector.example.org:4318
OTLP_LOGS_HEADERS=Authorization: Bearer <token>
```

Leave `LOKI_URL` out for an OTLP-only engine; with neither URL Vector runs with no log sink. Apply a change with
`systemctl restart vector` on the engine (or `enroll-engine.sh --vector-env FILE`; cloud-init writes the file from
`se-image/cloud-init/se-instance.yaml.tpl`, whose OTLP lines are commented examples). Values may not contain `"`, `\`
or `$`, and a malformed header makes the unit fail at start with a `sy-vector-sinks:` message (without the value).

Resource attributes on every OTLP record: `service.name` (`haproxy`, `coraza` or `sy-auth`), `tenant` (the id),
`tenant_name`, `se_group`, `engine` and `frontend` (`vs_<service>`, `sy_auth` or `waf`); the body is the log line. One
record goes out per request, so a high-volume engine sends many small requests.

Each sink has its own 10 000-event memory buffer. When a sink's buffer is full, the newest events for that sink are
discarded and the other sink is unaffected. Vector offers no drop-oldest policy (ADR 0221). Drops show on the engine at
`curl -s http://127.0.0.1:9598/metrics | grep buffer_discarded_events_total` (`component_id="loki"` or `"otlp"`).
Events that were never accepted by the collector (4xx/5xx after retries) show in `component_errors_total`.

**Grafana behind the controller ([adr/0085](../adr/0085-grafana-behind-controller.md)).** Off until set up; "Open in Grafana" then opens it in a new tab, one organization per tenant, and the controller rewrites every query to the tenant.
1. Grafana (this stack) must be reachable from the controller nodes and from nowhere else (`BIND` to the management address and a firewall). Set `GRAFANA_PROXY_WHITELIST` in `telemetry.env` to the controller addresses, IPv6 ones too if it reaches Grafana over IPv6 (otherwise Grafana answers 401 `auth-proxy.invalid-ip`) (the default, loopback, trusts nobody) and `GRAFANA_ROOT_URL` to `https://<ui address>/grafana/` (with the port if it is not 443); then `docker compose up -d grafana`.
2. On every controller node set `SWITCHYARD_GRAFANA_INTERNAL_URL=http://<grafana host>:3000`, `SWITCHYARD_GRAFANA_ADMIN_PASSWORD` (the value of `GRAFANA_ADMIN_PASSWORD`), `SWITCHYARD_GRAFANA_DS_SECRET` (`openssl rand -hex 32`, identical on all nodes), `SWITCHYARD_GRAFANA_DS_URL=http://<this node or VIP>:8000/internal/ds` (the address *Grafana* uses; do not route `/internal/ds` through the public front) and the Prometheus and Loki URLs; restart the API.
3. Check as a tenant viewer: `Open in Grafana` on a virtual service lands on the "Virtual services" dashboard with only that tenant's services; in Explore a query for another tenant's `tenant` label returns nothing. As an administrator: `POST /api/v1/grafana/launch`.
`401` on `/grafana/...` means no session (open it from the UI); `403` with code `grafana_ticket` means the link was used or expired; a panel error `query refused` is the controller declining a query it cannot scope (the message says why); `Grafana is not available` on launch is the controller failing to reach `INTERNAL_URL` or to log in with the admin password. A role change reaches Grafana within 30 minutes (the end of the session). Rotating `GRAFANA_DS_SECRET` re-keys the tenants at their next launch; until then their panels answer `401`.
Tested against these images with `SWITCHYARD_STACK_TESTS=1 pytest tests/integration/test_grafana_stack.py` (needs Docker; pulls Prometheus, Loki and Grafana).

**Retention and disk.** Metrics 30 days (`PROM_RETENTION`), logs 14 days (`loki.yaml`). Volumes: `prometheus-data`,
`loki-data`, `grafana-data`. Losing them loses history only.

**Proxmox host and guest metrics.** Create a read-only user and token on Proxmox (`pveum user add monitor@pve`,
`pveum aclmod / -user monitor@pve -role PVEAuditor`, `pveum user token add monitor@pve prometheus --privsep 0`), put
them in `telemetry.env` (`PVE_USER`, `PVE_TOKEN_NAME`, `PVE_TOKEN_VALUE`), copy
`prometheus/scrape.d/pve.yml.example` to `pve.yml` with your node address, then
`docker compose --profile pve up -d`. The "Proxmox" dashboard shows node CPU and memory, running and stopped guests
by type (qemu and lxc), top guests and storage use. Checked against the lab Proxmox with `sh deploy/telemetry/pve-smoke.sh`
and against two real nodes with six engines running: node, memory, storage and guest CPU/memory panels all return data.
