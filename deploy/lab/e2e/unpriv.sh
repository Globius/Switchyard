#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Trial of unprivileged containers as engines (lxc only). Reports what works; it does not fail the run, because the
# answer is information (docs/production.md says unprivileged containers are not supported until this passes).
set -eu
cd "$(dirname "$0")"
. ./lib.sh
getj() { python -c "import json,sys;d=json.load(sys.stdin);$1"; }
step "unprivileged container template"
UN=9963
docker exec $PVE env UNPRIVILEGED=1 sh /root/build/se-image/lxc/build-template.sh $UN /root/build/sy-agent /root/build/dataplaneapi > work/template-unpriv.log 2>&1   || { tail -15 work/template-unpriv.log; echo "RESULT unprivileged: the template build failed"; exit 0; }
step "deploy one engine from it, enroll, apply a service"
TEMPLATE_VMID=$UN api deploy edge-unpriv 1 > work/deploy-unpriv.json
sh enroll.sh work/deploy-unpriv.json >/dev/null
ok=0; n=0
while [ $n -lt 40 ]; do
  [ "$(api status | getj "print(sum(1 for e in d if e['engine']=='edge-unpriv-1' and e['reachable']))")" = 1 ] && { ok=1; break; }
  n=$((n+1)); sleep 3
done
if [ $ok = 1 ]; then
  R=$(api vs unpriv edge-unpriv 10.99.0.232 2>&1 | getj "print(d.get('state'), d.get('error') or '')" 2>&1 || true)
  if docker exec $PVE curl -s -m 3 -o /dev/null -w '%{http_code}' http://10.99.0.232/api/v1/healthz | grep -q 200; then
    echo "RESULT unprivileged: enrolled, applied ($R) and the VIP answers"
  else echo "RESULT unprivileged: enrolled but the service does not work ($R)"; fi
else
  echo "RESULT unprivileged: the engine never became reachable"
fi
