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

**Retention and disk.** Metrics 30 days (`PROM_RETENTION`), logs 14 days (`loki.yaml`). Volumes: `prometheus-data`,
`loki-data`, `grafana-data`. Losing them loses history only.

**Proxmox host and guest metrics.** Create a read-only user and token on Proxmox (`pveum user add monitor@pve`,
`pveum aclmod / -user monitor@pve -role PVEAuditor`, `pveum user token add monitor@pve prometheus --privsep 0`), put
them in `telemetry.env` (`PVE_USER`, `PVE_TOKEN_NAME`, `PVE_TOKEN_VALUE`), copy
`prometheus/scrape.d/pve.yml.example` to `pve.yml` with your node address, then
`docker compose --profile pve up -d`. The "Proxmox" dashboard shows node CPU and memory, running and stopped guests
by type (qemu and lxc), top guests and storage use. Checked against the lab Proxmox with `sh deploy/telemetry/pve-smoke.sh`
and against two real nodes with six engines running: node, memory, storage and guest CPU/memory panels all return data.
