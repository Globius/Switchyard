#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Issues the certificate a controller node uses for etcd (server, peer and client), Patroni's REST API and PostgreSQL from an
# OpenBao PKI role, into ./secrets/pki (ca.crt, node.crt, node.key). Run it where `bao` is logged in, once per node:
#   NODE_NAME=ctl-1 NODE_IP=10.0.0.11 sh issue-node-cert.sh  &&  scp -r secrets/pki ctl-1:/opt/switchyard/deploy/prod/secrets/
# PKI_MOUNT and PKI_ROLE default to pki and internal; the role must allow IP SANs and both server and client use.
# The key is made readable by uid 999 (postgres in the Patroni image); etcd runs as root.
set -eu
: "${NODE_NAME:?}" "${NODE_IP:?}"
MOUNT=${PKI_MOUNT:-pki}
ROLE=${PKI_ROLE:-internal}
OUT=${OUT_DIR:-secrets/pki}
mkdir -p "$OUT"
bao write -format=json "$MOUNT/issue/$ROLE" common_name="$NODE_NAME" ip_sans="$NODE_IP,127.0.0.1" ttl="${TTL:-8760h}" > "$OUT/.issue.json"
python3 - "$OUT" <<'PY'
import json, sys
out = sys.argv[1]
d = json.load(open(f"{out}/.issue.json"))["data"]
open(f"{out}/node.crt", "w").write(d["certificate"] + "\n")
open(f"{out}/node.key", "w").write(d["private_key"] + "\n")
open(f"{out}/ca.crt", "w").write(d["issuing_ca"] + "\n")
PY
rm -f "$OUT/.issue.json"
chmod 644 "$OUT/ca.crt" "$OUT/node.crt"
chown 999:999 "$OUT/node.key" 2>/dev/null || true
chmod 640 "$OUT/node.key"
echo "wrote $OUT/{ca.crt,node.crt,node.key} for $NODE_NAME ($NODE_IP)"
