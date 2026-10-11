#!/bin/sh
# End-to-end lab: controller + OpenBao PKI + PostgreSQL on the host's Docker, a real Proxmox VE (dockur/proxmox) and
# an SE deployed as an LXC container. Proves: SE template build, deploy job, enrollment over https, renewal,
# mTLS to the Data Plane API, an applied change set (canary included) and traffic through a VRRP VIP.
# Needs: the dev PostgreSQL (deploy/dev) and OpenBao (container sy-openbao), `uv`, Docker. About 8 minutes.
# Brings up everything except the scenario: sh deploy/lab/e2e/base.sh
set -eu
cd "$(dirname "$0")"
. ./lib.sh

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


# The Proxmox container is the client and the pool member: give it an address on each lab network.
docker exec $PVE ip addr add 10.99.0.1/24 dev vmbr0 2>/dev/null || true
docker exec $PVE ip addr add 10.98.0.1/24 dev vmbr0 2>/dev/null || true
# The pool member: a plain web server on the backend network, independent of the controller (so the lab can stop the
# controller and still have a healthy backend).
docker exec $PVE mkdir -p /srv/www/api/v1
docker exec $PVE sh -c 'echo ok > /srv/www/api/v1/healthz'
docker exec -d $PVE python3 -m http.server 8001 --bind 10.98.0.1 --directory /srv/www
echo "base ready"
