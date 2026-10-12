#!/bin/sh
# Runs the lab driver (deploy/lab/e2e/drive.py, read from stdin) inside the controller's api container. Install it on the
# machine that runs the quickstart stack as /usr/local/bin/sy-api and point CONTROLLER_EXEC at it (see lib.sh).
TENANT=lab; [ "${1:-}" = --tenant ] && { TENANT=$2; shift 2; }
cd "${QUICKSTART_DIR:-/opt/switchyard/deploy/quickstart}" || exit 1
TOK=$(cat secrets/admin-token)
exec docker compose --env-file .env exec -T -e SY_TOKEN="$TOK" -e LAB_URL=http://127.0.0.1:8000 -e LAB_CA=/etc/ssl/certs/ca-certificates.crt -e CRED_REF=kv/se/dataplane -e LAB_TENANT="$TENANT" api python - "$@"
