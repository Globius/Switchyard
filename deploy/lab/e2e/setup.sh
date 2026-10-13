#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Prepares everything the end-to-end lab needs besides the Proxmox container: a database, an OpenBao PKI and
# AppRole for the controller, certificates, a bundle signing key and the work files. Idempotent enough to rerun
# after `teardown.sh`. Needs the dev PostgreSQL (deploy/dev) and the dev OpenBao (container sy-openbao).
set -eu
cd "$(dirname "$0")"
W=work; mkdir -p $W
BAO() { docker exec -i -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN=devroot sy-openbao bao "$@"; }
export MSYS_NO_PATHCONV=1

docker exec switchyard-dev-postgres-1 psql -U postgres -c "create database e2e" 2>/dev/null || true

BAO secrets disable pki-e2e >/dev/null 2>&1 || true   # start from a fresh CA every time
BAO secrets enable -path=pki-e2e pki >/dev/null
BAO secrets tune -max-lease-ttl=87600h pki-e2e >/dev/null
BAO write -field=certificate pki-e2e/root/generate/internal common_name="Switchyard e2e CA" ttl=87600h > $W/ca.pem 2>/dev/null
echo >> $W/ca.pem
# use_csr_sans=false: the names come from the controller (engine name and management address), never from
# the agent's CSR (ADR 0011). With the default (true) OpenBao ignores the controller's ip_sans.
BAO write pki-e2e/roles/agents allow_any_name=true allow_ip_sans=true enforce_hostnames=false \
  key_type=any use_csr_sans=false use_csr_common_name=false max_ttl=168h ttl=24h >/dev/null

BAO write pki-e2e/roles/internal allow_any_name=true allow_ip_sans=true enforce_hostnames=false \
  key_type=ec key_bits=256 max_ttl=168h ttl=24h >/dev/null

# Controller server certificate (https for enrollment) and client certificate (mTLS to agents).
issue() { # name cn ip_sans
  BAO write -format=json pki-e2e/issue/internal common_name="$2" ip_sans="$3" ttl=24h > $W/$1.json 2>/dev/null
  python -c "
import json
d=json.load(open('$W/$1.json'))['data']
open('$W/$1.crt','w',newline='\n').write(d['certificate']+'\n'+d['issuing_ca']+'\n')
open('$W/$1.key','w',newline='\n').write(d['private_key']+'\n')
"
}
issue server controller 172.30.0.1,127.0.0.1
issue client switchyard-controller 127.0.0.1

# Bundle signing key (Ed25519, PKCS#8 PEM).
python -c "
from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
k=Ed25519PrivateKey.generate()
open('$W/bundle.key','wb').write(k.private_bytes(s.Encoding.PEM,s.PrivateFormat.PKCS8,s.NoEncryption()))
"

# Controller policy: the generated secret rules plus signing certificates from the e2e PKI.
(cd ../../../controller && uv run syctl openbao-policy --shared kv/se/) > $W/policy.hcl
printf '\npath "pki-e2e/sign/agents" { capabilities = ["update"] }\n' >> $W/policy.hcl
BAO policy write e2e-controller - < $W/policy.hcl >/dev/null
BAO auth enable approle >/dev/null 2>&1 || true
BAO write auth/approle/role/e2e-controller token_policies=e2e-controller >/dev/null
BAO read -field=role_id auth/approle/role/e2e-controller/role-id > $W/role_id
BAO write -f -field=secret_id auth/approle/role/e2e-controller/secret-id > $W/secret_id

# Shared Data Plane credentials: SEs get the password through enroll.env, the controller reads this secret.
PW=$(python -c "import secrets;print(secrets.token_hex(12))")
printf '%s' "$PW" > $W/dp_password
BAO kv put kv/se/e2e-dpapi value="switchyard:$PW" >/dev/null
echo "setup done: $W"
