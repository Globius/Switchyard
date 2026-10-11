#!/bin/sh
# Starts the controller (api, worker, TLS front) inside the Proxmox container's network namespace, so it can reach
# the SE containers on the Proxmox bridge (172.30.0.0/24, gateway 172.30.0.1). Run setup.sh and bring up
# deploy/lab/pve first. Prints nothing secret.
set -eu
cd "$(dirname "$0")"
export MSYS_NO_PATHCONV=1
PVE=switchyard-pve-lab-pve-1
WORK=$(pwd -W 2>/dev/null || pwd)/work
IMG=${CONTROLLER_IMAGE:-switchyard-controller:dev}
eval "$(sh ../pve/bootstrap.sh)"      # SWITCHYARD_TEST_PVE_TOKEN_ID / SWITCHYARD_TEST_PVE_SECRET
docker exec $PVE cat /etc/pve/pve-root-ca.pem > work/pve-ca.pem
VAR=$(docker run --rm --entrypoint python $IMG -c "from switchyard.adapters.secrets import EnvSecrets;print(EnvSecrets.variable_name('kv/proxmox/token'))")

ENVS="-e SWITCHYARD_DATABASE_URL=postgresql+psycopg://postgres:postgres@host.docker.internal:5432/e2e
 -e SWITCHYARD_PUBLIC_URL=https://172.30.0.1:8443
 -e SWITCHYARD_TRUST_FORWARDED=1
 -e SWITCHYARD_OPENBAO_ADDR=http://host.docker.internal:18200
 -e SWITCHYARD_OPENBAO_ROLE_ID=$(cat work/role_id)
 -e SWITCHYARD_OPENBAO_SECRET_ID=$(cat work/secret_id)
 -e SWITCHYARD_PKI_MOUNT=pki-e2e -e SWITCHYARD_PKI_ROLE=agents
 -e SWITCHYARD_BUNDLE_KEY_FILE=/e2e/bundle.key
 -e SWITCHYARD_DATAPLANE_CA=/e2e/ca.pem -e SWITCHYARD_DATAPLANE_CERT=/e2e/client.crt -e SWITCHYARD_DATAPLANE_KEY=/e2e/client.key
 -e SWITCHYARD_PVE_URL=https://127.0.0.1:8006 -e SWITCHYARD_PVE_TOKEN_ID=$SWITCHYARD_TEST_PVE_TOKEN_ID
 -e SWITCHYARD_PVE_CA=/e2e/pve-ca.pem -e $VAR=$SWITCHYARD_TEST_PVE_SECRET
 -e SWITCHYARD_ENGINE_CIDRS=172.30.0.0/24 -e SWITCHYARD_SECRET_POLICY=open"
for n in sy-e2e-api sy-e2e-worker sy-e2e-tls; do docker rm -f $n >/dev/null 2>&1 || true; done
# shellcheck disable=SC2086
docker run -d --name sy-e2e-api --network container:$PVE -v "$WORK:/e2e:ro" $ENVS $IMG serve --host 0.0.0.0 --port 8000 >/dev/null
# shellcheck disable=SC2086
docker run -d --name sy-e2e-worker --network container:$PVE -v "$WORK:/e2e:ro" $ENVS $IMG worker >/dev/null
cat > work/nginx.conf <<'NGINX'
server {
    listen 8443 ssl;
    ssl_certificate /e2e/server.crt;
    ssl_certificate_key /e2e/server.key;
    location / { proxy_pass http://127.0.0.1:8000; proxy_set_header X-Forwarded-For $remote_addr; }
}
NGINX
docker run -d --name sy-e2e-tls --network container:$PVE -v "$WORK:/e2e:ro" -v "$WORK/nginx.conf:/etc/nginx/conf.d/default.conf:ro" nginx:1.27-alpine >/dev/null
n=0; until docker exec sy-e2e-api python -c "import urllib.request as u;u.urlopen('http://127.0.0.1:8000/api/v1/healthz',timeout=2)" 2>/dev/null; do
  n=$((n+1)); [ $n -lt 30 ] || { docker logs sy-e2e-api | tail -20; exit 1; }; sleep 2; done
echo "controller up"
