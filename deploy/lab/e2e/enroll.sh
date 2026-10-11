#!/bin/sh
# Delivers enrollment material to every deployed container that has a token in the deploy output (what cloud-init
# does for a VM): controller URL and CN, Data Plane credentials, the one-time token and the controller CA, then runs
# the enrollment unit. Usage: enroll.sh deploy-output.json
set -eu
cd "$(dirname "$0")"
export MSYS_NO_PATHCONV=1
PVE=switchyard-pve-lab-pve-1
printf 'CONTROLLER_URL=https://172.30.0.1:8443\nCONTROLLER_CN=switchyard-controller\nDP_USER=switchyard\nDP_PASSWORD=%s\n' "$(cat work/dp_password)" > work/enroll.env
docker cp work/enroll.env $PVE:/root/enroll.env
docker cp work/ca.pem $PVE:/root/controller-ca.crt
python -c "
import json,sys
for e in json.load(open(sys.argv[1]))['engines']:
    if 'token' in e: print(e['vmid'], e['token']['token'])
" "$1" | while read -r VMID TOKEN; do
  printf '%s\n' "$TOKEN" > work/enroll.token
  docker cp work/enroll.token $PVE:/root/enroll.token
  docker exec $PVE pct push "$VMID" /root/enroll.env /etc/switchyard/agent/enroll.env --perms 600
  docker exec $PVE pct push "$VMID" /root/enroll.token /etc/switchyard/agent/enroll.token --perms 600
  docker exec $PVE pct push "$VMID" /root/controller-ca.crt /etc/switchyard/agent/controller-ca.crt --perms 644
  docker exec $PVE pct exec "$VMID" -- systemctl start sy-agent-enroll.service
  echo "enrolled container $VMID"
done
