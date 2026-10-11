#!/bin/sh
# End-to-end lab: controller + OpenBao PKI + PostgreSQL on the host's Docker, a real Proxmox VE (dockur/proxmox) and
# an SE deployed as an LXC container. Proves: SE template build, deploy job, enrollment over https, renewal,
# mTLS to the Data Plane API, an applied change set (canary included) and traffic through a VRRP VIP.
# Needs: the dev PostgreSQL (deploy/dev) and OpenBao (container sy-openbao), `uv`, Docker. About 8 minutes.
#   sh deploy/lab/e2e/run.sh        then: sh deploy/lab/e2e/teardown.sh
set -eu
cd "$(dirname "$0")"
export MSYS_NO_PATHCONV=1
PVE=switchyard-pve-lab-pve-1
TEMPLATE=9961
fail() { echo "E2E FAIL: $*" >&2; exit 1; }
step() { echo "== $*"; }
api() { docker exec -i -e SY_TOKEN="$(cat work/token)" -e TEMPLATE_VMID=$TEMPLATE sy-e2e-api python - "$1" < drive.py; }

sh teardown.sh >/dev/null 2>&1 || true   # start clean

step "controller image and SE binaries"
(cd ../../.. && docker build -q -f deploy/controller/Dockerfile -t switchyard-controller:dev . >/dev/null)
(cd ../../.. && docker build -q -f se-image/test/Dockerfile -t sy-se-image-test . >/dev/null)   # also runs the image checks
mkdir -p work
cid=$(docker create sy-se-image-test)
docker cp "$cid:/usr/local/bin/sy-agent" work/sy-agent
docker cp "$cid:/usr/bin/dataplaneapi" work/dataplaneapi
docker rm "$cid" >/dev/null

step "Proxmox lab and the container template"
docker compose -f ../pve/compose.yaml down -v >/dev/null 2>&1 || true
docker compose -f ../pve/compose.yaml up -d >/dev/null
n=0; until docker exec $PVE pvesh get /version >/dev/null 2>&1; do n=$((n+1)); [ $n -lt 60 ] || fail "Proxmox did not start"; sleep 5; done
docker exec $PVE mkdir -p /root/build
docker cp ../../../se-image/. $PVE:/root/build/se-image/
docker cp work/sy-agent $PVE:/root/build/sy-agent
docker cp work/dataplaneapi $PVE:/root/build/dataplaneapi
docker exec $PVE sh /root/build/se-image/lxc/build-template.sh $TEMPLATE /root/build/sy-agent /root/build/dataplaneapi >work/template-build.log 2>&1 \
  || { tail -20 work/template-build.log; fail "template build"; }

step "OpenBao, certificates, database, controller"
sh setup.sh >/dev/null
DB=postgresql+psycopg://postgres:postgres@host.docker.internal:5432/e2e
docker run --rm -e SWITCHYARD_DATABASE_URL=$DB switchyard-controller:dev db upgrade >/dev/null
docker run --rm -e SWITCHYARD_DATABASE_URL=$DB -v "$(pwd -W 2>/dev/null || pwd)/tenant.yaml:/tenant.yaml:ro" switchyard-controller:dev adopt /tenant.yaml
docker run --rm -e SWITCHYARD_DATABASE_URL=$DB switchyard-controller:dev token --name e2e 2>&1 | grep -o 'sy_[A-Za-z0-9_-]*' | tail -1 > work/token
[ -s work/token ] || fail "no API token"
sh up.sh >/dev/null

step "deploy an LXC engine through the controller"
api deploy > work/deploy.json
python -c "import json;d=json.load(open('work/deploy.json'));assert 'engine' in d, d" || fail "deploy: $(cat work/deploy.json)"

step "enroll it (https), then renew"
sh enroll.sh >/dev/null
sleep 5
docker exec $PVE pct exec "$(python -c "import json;print(json.load(open('work/deploy.json'))['engine']['vmid'])")" -- systemctl start sy-agent-renew.service
n=0; until api status 2>&1 | grep -q '"reachable": true'; do n=$((n+1)); [ $n -lt 20 ] || { api status | cut -c1-600; fail "engine not reachable"; }; sleep 5; done

step "apply a virtual service (render, validate, canary, push) and send traffic through the VIP"
api vs > work/vs.json
python -c "import json;d=json.load(open('work/vs.json'));assert d.get('name')=='api', d" || { cat work/vs.json | cut -c1-600; fail "virtual service apply"; }
n=0; until [ "$(docker exec $PVE curl -s -m 5 -o /dev/null -w '%{http_code}' http://172.30.0.240/api/v1/healthz)" = 200 ]; do
  n=$((n+1)); [ $n -lt 12 ] || fail "no 200 through the VIP"; sleep 3; done
n=0; until api status | grep -q '"status": "UP"'; do n=$((n+1)); [ $n -lt 20 ] || { api status | cut -c1-700; fail "backend member is not UP in the engine status"; }; sleep 3; done
n=0; until api status | grep -q '"state": "MASTER"'; do n=$((n+1)); [ $n -lt 20 ] || { api status | cut -c1-700; fail "VRRP is not MASTER"; }; sleep 3; done
api status | grep -q '"drift": \[\]' || fail "engine reports drift"
echo "E2E PASSED: deploy, enroll, renew, mTLS, apply, VIP traffic"
