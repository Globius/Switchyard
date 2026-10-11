#!/bin/sh
# Removes everything run.sh started (controller containers, Proxmox lab, OpenBao PKI objects, e2e database).
cd "$(dirname "$0")"
export MSYS_NO_PATHCONV=1
for n in sy-e2e-api sy-e2e-worker sy-e2e-tls; do docker rm -f $n >/dev/null 2>&1; done
docker compose -f ../pve/compose.yaml down -v >/dev/null 2>&1
B="docker exec -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN=devroot sy-openbao bao"
$B secrets disable pki-e2e >/dev/null 2>&1
$B delete auth/approle/role/e2e-controller >/dev/null 2>&1
$B policy delete e2e-controller >/dev/null 2>&1
$B kv metadata delete kv/se/e2e-dpapi >/dev/null 2>&1
docker exec switchyard-dev-postgres-1 psql -U postgres -c "drop database if exists e2e" >/dev/null 2>&1
echo "e2e lab removed"
