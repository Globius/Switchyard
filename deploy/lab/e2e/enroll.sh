#!/bin/sh
# Delivers enrollment material to the deployed container (what cloud-init does for a VM): controller URL, Data Plane
# credentials, the one-time token and the controller CA, then runs the enrollment unit.
set -eu
cd "$(dirname "$0")"
export MSYS_NO_PATHCONV=1
PVE=switchyard-pve-lab-pve-1
VMID=$(python -c "import json;print(json.load(open('work/deploy.json'))['engine']['vmid'])")
TOKEN=$(python -c "import json;print(json.load(open('work/deploy.json'))['token']['token'])")
printf 'CONTROLLER_URL=https://172.30.0.1:8443\nDP_USER=switchyard\nDP_PASSWORD=%s\n' "$(cat work/dp_password)" > work/enroll.env
printf '%s\n' "$TOKEN" > work/enroll.token
docker cp work/enroll.env $PVE:/root/enroll.env
docker cp work/enroll.token $PVE:/root/enroll.token
docker cp work/ca.pem $PVE:/root/controller-ca.crt
docker exec $PVE sh -c "
pct push $VMID /root/enroll.env /etc/switchyard/agent/enroll.env --perms 600
pct push $VMID /root/enroll.token /etc/switchyard/agent/enroll.token --perms 600
pct push $VMID /root/controller-ca.crt /etc/switchyard/agent/controller-ca.crt --perms 644
pct exec $VMID -- systemctl start sy-agent-enroll.service
sleep 8
pct exec $VMID -- sh -c 'systemctl is-active sy-agent-enroll sy-agent dataplaneapi haproxy; ls /etc/switchyard/agent'
"
