#!/bin/sh
# Live checks (.p12 import and the 412 conflict) against the controller of the lab (hosts.env, lib.sh). Usage: sh live.sh
set -eu
cd "$(dirname "$0")/../hosts"
[ -f hosts.env ] && . ./hosts.env
. ./lib.sh
export DRIVER=../checks/live.py LAB_TENANT=${LAB_TENANT:-lab}
step "PKCS#12 import, wrong password, replace"; api p12
step "stale and missing If-Match"; api conflict
echo "live checks passed"
