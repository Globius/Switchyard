# Telemetry stack (Prometheus, Loki, Grafana)

Setup: `deploy/telemetry/`. Smoke test: `sh deploy/telemetry/smoke.sh` (needs the dev PostgreSQL and a built controller
image; checks the scrape, discovery, Loki and Grafana provisioning).

**Install.** Copy `telemetry.env.example` to `telemetry.env`, put the value of `SWITCHYARD_METRICS_TOKEN` in
`secrets/metrics.token`, then `docker compose --env-file telemetry.env up -d`. On the controller set
`SWITCHYARD_PROMETHEUS_URL=http://<host>:9090` and `SWITCHYARD_LOKI_URL=http://<host>:3100` and restart the API;
the UI charts and log tail stop saying "no data source".

**Checks when something looks empty.**
1. Prometheus `Status > Targets`: `switchyard`, `se-haproxy`, `se-node` should be up. An empty `se-*` job means
   no enabled engine has a management address, or the token is wrong (`401` in the target error).
2. `se-haproxy` down but `se-node` up: the SE group has no `mgmt` network, so no exporter listener is rendered
   (ADR 0063), or the scrape comes from outside that network.
3. Logs missing: on the SE `systemctl status vector` (it only runs once `/etc/switchyard/vector.env` exists);
   `journalctl -u vector`. In Grafana Explore, `{tenant_name="…"}` should show lines.
4. UI shows another tenant's or no data: compare the `tenant` label on the SE's series with the tenant id.

**Retention and disk.** Metrics 30 days (`PROM_RETENTION`), logs 14 days (`loki.yaml`). Volumes: `prometheus-data`,
`loki-data`, `grafana-data`. Losing them loses history only.

**Proxmox host and guest metrics.** Create a read-only user and token on Proxmox (`pveum user add monitor@pve`,
`pveum aclmod / -user monitor@pve -role PVEAuditor`, `pveum user token add monitor@pve prometheus --privsep 0`), put
them in `telemetry.env` (`PVE_USER`, `PVE_TOKEN_NAME`, `PVE_TOKEN_VALUE`), copy
`prometheus/scrape.d/pve.yml.example` to `pve.yml` with your node address, then
`docker compose --profile pve up -d`. The "Proxmox" dashboard shows node CPU and memory, running and stopped guests
by type (qemu and lxc), top guests and storage use. Checked against the lab Proxmox with `sh deploy/telemetry/pve-smoke.sh`
(node, memory and storage series appear; no guests existed, so the guest panels are untested with data).
