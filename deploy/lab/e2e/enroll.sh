#!/bin/sh
# Delivers enrollment material to every deployed container that has a token in the deploy output (what cloud-init
# does for a VM): controller URL and CN, Data Plane credentials, the one-time token and the controller CA, then runs
# the enrollment unit. Usage: enroll.sh deploy-output.json
# The secrets travel through stdin straight into the container's files (mode 600): no copy is left in work/ or on
# the Proxmox node, and the tokens are removed from the deploy output when done (finding S5).
set -eu
cd "$(dirname "$0")"
export MSYS_NO_PATHCONV=1
PVE=switchyard-pve-lab-pve-1
DEST=/etc/switchyard/agent
KIND=${GUEST_KIND:-lxc}
put() { # vmid path mode  (content on stdin)
  if [ "$KIND" = lxc ]; then docker exec -i $PVE pct exec "$1" -- sh -c "umask 077; cat > $2; chmod $3 $2"
  else docker exec -i $PVE qm guest exec "$1" --pass-stdin 1 -- sh -c "umask 077; cat > $2; chmod $3 $2" >/dev/null; fi
}
run() { # vmid command...
  vmid=$1; shift
  if [ "$KIND" = lxc ]; then docker exec $PVE pct exec "$vmid" -- "$@"; else docker exec $PVE qm guest exec "$vmid" -- "$@" >/dev/null; fi
}
python -c "
import json,sys
for e in json.load(open(sys.argv[1]))['engines']:
    if 'token' in e: print(e['vmid'], e['token']['token'])
" "$1" | while read -r VMID TOKEN; do
  if [ "$KIND" = qemu ]; then   # a freshly cloned VM needs a minute before its guest agent answers
    n=0; until docker exec $PVE qm agent "$VMID" ping >/dev/null 2>&1; do n=$((n+1)); [ $n -lt 60 ] || { echo "guest agent of VM $VMID never answered" >&2; exit 1; }; sleep 3; done
  fi
  printf 'CONTROLLER_URL=https://172.30.0.1:8443\nCONTROLLER_CN=switchyard-controller\nDP_USER=switchyard\nDP_PASSWORD=%s\n' \
    "$(cat work/dp_password)" | put "$VMID" $DEST/enroll.env 600
  printf '%s\n' "$TOKEN" | put "$VMID" $DEST/enroll.token 600
  put "$VMID" $DEST/controller-ca.crt 644 < work/ca.pem
  run "$VMID" systemctl start sy-agent-enroll.service
  echo "enrollment started in guest $VMID"
done
# The tokens are single-use; do not keep them around.
python -c "
import json,sys
p=sys.argv[1]
d=json.load(open(p))
for e in d['engines']: e.pop('token',None)
json.dump(d,open(p,'w'))
" "$1"
