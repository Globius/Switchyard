#!/bin/sh
# Points the controller at your Proxmox cluster and checks that it can log in. Usage:
#   sh connect-proxmox.sh --url https://pve1.example:8006 --token-id switchyard@pve!controller --secret SECRET [--ca pve-root-ca.pem]
# --ca is the Proxmox root CA (/etc/pve/pve-root-ca.pem on a node); without it the controller verifies against public CAs.
set -eu
cd "$(dirname "$0")"
URL=""; TID=""; SEC=""; CA=""
while [ $# -gt 0 ]; do case "$1" in
  --url) URL=$2; shift 2 ;; --token-id) TID=$2; shift 2 ;; --secret) SEC=$2; shift 2 ;; --ca) CA=$2; shift 2 ;;
  *) echo "see the header of this script" >&2; exit 2 ;; esac; done
[ -n "$URL" ] && [ -n "$TID" ] && [ -n "$SEC" ] || { echo "--url, --token-id and --secret are required" >&2; exit 2; }
ENVSET() { grep -q "^$1=" .env && sed -i "s#^$1=.*#$1=$2#" .env || echo "$1=$2" >> .env; }
ENVSET PVE_URL "$URL"; ENVSET PVE_TOKEN_ID "$TID"; ENVSET PVE_TOKEN_SECRET "$SEC"
if [ -n "$CA" ]; then cp "$CA" secrets/pve-ca.pem; chmod 644 secrets/pve-ca.pem; ENVSET PVE_CA_FILE /secrets/pve-ca.pem; fi
export MSYS_NO_PATHCONV=1
docker compose --env-file .env up -d api worker >/dev/null
echo "checking the Proxmox login..."
n=0; until docker compose --env-file .env exec -T api python -c "
import asyncio
from switchyard.runtime import proxmox_client
async def go():
    client = proxmox_client()
    assert client is not None, 'Proxmox settings missing'
    nodes = await client.list_nodes()
    print('Proxmox nodes:', ', '.join(f'{n.name} ({\"online\" if n.online else \"offline\"})' for n in nodes))
    await client.close()
asyncio.run(go())
" 2>&1 | tee /dev/stderr | grep -q "Proxmox nodes"; do
  n=$((n+1)); [ $n -lt 5 ] || { echo "could not log in to Proxmox: check the URL, token and CA" >&2; exit 1; }; sleep 3; done
