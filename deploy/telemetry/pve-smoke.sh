#!/bin/sh
# Proxmox metrics end to end: the lab Proxmox (deploy/lab/pve), prometheus-pve-exporter, Prometheus, Grafana.
set -eu
cd "$(dirname "$0")"
LAB=../lab/pve/compose.yaml
docker compose -f "$LAB" up -d >/dev/null
eval "$(sh ../lab/pve/bootstrap.sh)"        # exports SWITCHYARD_TEST_PVE_TOKEN_ID / _SECRET
USER_PART=${SWITCHYARD_TEST_PVE_TOKEN_ID%%!*}; NAME_PART=${SWITCHYARD_TEST_PVE_TOKEN_ID##*!}
mkdir -p secrets; printf x > secrets/metrics.token
cat > prometheus/scrape.d/pve.yml <<'YML'
scrape_configs:
  - job_name: pve
    metrics_path: /pve
    params: { module: [default], cluster: ["1"], node: ["1"] }
    static_configs: [{ targets: ["host.docker.internal:18006"] }]
    relabel_configs:
      - { source_labels: [__address__], target_label: __param_target }
      - { source_labels: [__param_target], target_label: instance }
      - { target_label: __address__, replacement: pve-exporter:9221 }
YML
cleanup() {
  CONTROLLER_HOSTPORT=x GRAFANA_ADMIN_PASSWORD=x docker compose --profile pve down -v >/dev/null 2>&1
  docker compose -f "$LAB" down -v >/dev/null 2>&1; rm -rf secrets prometheus/scrape.d/pve.yml
}
trap cleanup EXIT
export CONTROLLER_SCHEME=http CONTROLLER_HOSTPORT=host.docker.internal:9 GRAFANA_ADMIN_PASSWORD=smoke
export PVE_USER=$USER_PART PVE_TOKEN_NAME=$NAME_PART PVE_TOKEN_VALUE=$SWITCHYARD_TEST_PVE_SECRET PVE_VERIFY_SSL=false
docker compose --profile pve up -d >/dev/null
wait_for() { n=0; until eval "$2" >/dev/null 2>&1; do n=$((n+1)); [ $n -lt 40 ] || { echo "FAIL: $1"; docker compose --profile pve logs --tail 20 pve-exporter prometheus; exit 1; }; sleep 3; done; echo "ok: $1"; }
Q() { curl -sf "localhost:9090/api/v1/query" --data-urlencode "query=$1"; }
wait_for "pve node is up in Prometheus" "Q 'pve_up{id=~\"node/.*\"}' | grep -q '\"1\"\]'"
wait_for "node memory series exist" "Q 'pve_memory_size_bytes{id=~\"node/.*\"}' | grep -q node/"
wait_for "storage series exist" "Q 'pve_disk_size_bytes{id=~\"storage/.*\"}' | grep -q storage/"
wait_for "proxmox dashboard is provisioned" "curl -sf -u admin:smoke 'localhost:3000/api/search?query=Proxmox' | grep -q sy-proxmox"
echo "pve telemetry smoke passed"
