#!/bin/sh
# Waits for the lab Proxmox API, creates an API token for root@pam and prints the environment variables
# the live adapter test reads. Run after `docker compose up -d`.
set -eu
URL=https://127.0.0.1:18006
n=0; until curl -sk -o /dev/null -w '%{http_code}' "$URL/api2/json/version" | grep -q 401; do
  n=$((n+1)); [ $n -lt 60 ] || { echo "Proxmox API did not come up" >&2; exit 1; }; sleep 5; done
T=$(curl -sk -d username=root@pam -d password=lab-root-password "$URL/api2/json/access/ticket")
TICKET=$(echo "$T" | sed 's/.*"ticket":"\([^"]*\)".*/\1/')
CSRF=$(echo "$T" | sed 's/.*"CSRFPreventionToken":"\([^"]*\)".*/\1/')
NAME=sy$(date +%s)
R=$(curl -sk -b "PVEAuthCookie=$TICKET" -H "CSRFPreventionToken: $CSRF" -X POST \
  "$URL/api2/json/access/users/root@pam/token/$NAME" -d privsep=0)
SECRET=$(echo "$R" | sed 's/.*"value":"\([^"]*\)".*/\1/')
echo "export SWITCHYARD_TEST_PVE_URL=$URL"
echo "export SWITCHYARD_TEST_PVE_TOKEN_ID=root@pam!$NAME"
echo "export SWITCHYARD_TEST_PVE_SECRET=$SECRET"
