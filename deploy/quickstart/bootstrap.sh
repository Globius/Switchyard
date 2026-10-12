#!/bin/sh
# One-command setup of a Switchyard test instance on this host (docs/quickstart.md). Idempotent: run it again to
# bring the stack back up; it keeps the passwords, keys and certificates it already made in .env and secrets/.
#   sh bootstrap.sh --host ADDRESS [--port 8443]
# ADDRESS is how browsers and engines reach this machine (an IP or DNS name); --port is the HTTPS port to publish.
set -eu
cd "$(dirname "$0")"
HOST=""; PORTARG=""
while [ $# -gt 0 ]; do case "$1" in --host) HOST=$2; shift 2 ;; --port) PORTARG=$2; shift 2 ;; *) echo "usage: sh bootstrap.sh --host ADDRESS [--port 8443]" >&2; exit 2 ;; esac; done
[ -n "$HOST" ] || [ -f .env ] || { echo "usage: sh bootstrap.sh --host ADDRESS" >&2; exit 2; }
export MSYS_NO_PATHCONV=1
say() { echo "== $*"; }
rand() { openssl rand -hex 16; }
ENVSET() { grep -q "^$1=" .env 2>/dev/null && sed -i "s#^$1=.*#$1=$2#" .env || echo "$1=$2" >> .env; }
ENVGET() { sed -n "s/^$1=//p" .env 2>/dev/null | head -1; }

mkdir -p secrets; chmod 755 secrets   # the controller containers (uid 10001) read it; keep it on an encrypted disk
[ -f .env ] || : > .env
[ -z "$HOST" ] || ENVSET HOST_ADDR "$HOST"
[ -z "$PORTARG" ] || ENVSET HTTPS_PORT "$PORTARG"
HOST=$(ENVGET HOST_ADDR)
[ -n "$(ENVGET POSTGRES_PASSWORD)" ] || ENVSET POSTGRES_PASSWORD "$(rand)"
[ -n "$(ENVGET METRICS_TOKEN)" ] || ENVSET METRICS_TOKEN "$(rand)"
[ -n "$(ENVGET BREAK_GLASS)" ] || ENVSET BREAK_GLASS 1
[ -n "$(ENVGET ALLOW_LXC)" ] || ENVSET ALLOW_LXC 1   # test instance: allow container engines (production: off, docs/production.md)
IMG=$(ENVGET CONTROLLER_IMAGE); IMG=${IMG:-switchyard-controller:latest}
dc() { docker compose --env-file .env "$@"; }
BAO() { dc exec -T -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN="$(cat secrets/root.token)" openbao bao "$@"; }
PY() { docker run --rm -i --user 0 -v "$(pwd -W 2>/dev/null || pwd)/secrets:/secrets" --entrypoint python "$IMG" - "$@"; }

say "controller image"
# --network host: the build uses the host's own network, so it also works on a host with only IPv6.
docker image inspect "$IMG" >/dev/null 2>&1 || docker build --network host -q -f ../controller/Dockerfile -t "$IMG" ../.. >/dev/null

say "PostgreSQL and OpenBao"
dc up -d postgres openbao >/dev/null
n=0; until dc exec -T -e BAO_ADDR=http://127.0.0.1:8200 openbao bao status >/dev/null 2>&1 || [ $? = 2 ]; do n=$((n+1)); [ $n -lt 30 ] || { echo "OpenBao did not start" >&2; exit 1; }; sleep 2; done
if ! dc exec -T -e BAO_ADDR=http://127.0.0.1:8200 openbao bao status 2>/dev/null | grep -q 'Initialized *true'; then
  dc exec -T -e BAO_ADDR=http://127.0.0.1:8200 openbao bao operator init -key-shares=1 -key-threshold=1 -format=json > secrets/openbao-init.json
  PY <<'PYEOF'
import json
d = json.load(open("/secrets/openbao-init.json"))
open("/secrets/unseal.key", "w").write(d["unseal_keys_b64"][0])
open("/secrets/root.token", "w").write(d["root_token"])
PYEOF
  chmod 600 secrets/openbao-init.json secrets/unseal.key secrets/root.token
fi
dc exec -T -e BAO_ADDR=http://127.0.0.1:8200 openbao bao operator unseal "$(cat secrets/unseal.key)" >/dev/null 2>&1 || true
dc up -d openbao-unseal >/dev/null
n=0; until BAO status 2>/dev/null | grep -q 'Sealed *false'; do n=$((n+1)); [ $n -lt 30 ] || { echo "OpenBao stays sealed" >&2; exit 1; }; sleep 2; done

say "OpenBao: secrets engine, PKI, controller policy and AppRole"
if [ ! -s secrets/ca.pem ]; then
  BAO secrets enable -path=kv -version=2 kv >/dev/null 2>&1 || true
  BAO secrets enable -path=pki pki >/dev/null
  BAO secrets tune -max-lease-ttl=87600h pki >/dev/null
  BAO write -field=certificate pki/root/generate/internal common_name="Switchyard test CA" ttl=87600h > secrets/ca.pem
  echo >> secrets/ca.pem
  # The names in an agent certificate come from the controller, never from the agent's request (ADR 0011, 0056).
  BAO write pki/roles/agents allow_any_name=true allow_ip_sans=true enforce_hostnames=false key_type=any \
    use_csr_sans=false use_csr_common_name=false max_ttl=168h ttl=24h >/dev/null
  BAO write pki/roles/internal allow_any_name=true allow_ip_sans=true enforce_hostnames=false key_type=ec key_bits=256 \
    max_ttl=168h ttl=24h >/dev/null
fi
docker run --rm --entrypoint syctl "$IMG" openbao-policy --shared kv/se/ > secrets/policy.hcl
printf '\npath "pki/sign/agents" { capabilities = ["update"] }\n' >> secrets/policy.hcl
BAO policy write switchyard-controller - < secrets/policy.hcl >/dev/null
BAO auth enable approle >/dev/null 2>&1 || true
BAO write auth/approle/role/switchyard-controller token_policies=switchyard-controller >/dev/null
if [ -z "$(ENVGET OPENBAO_ROLE_ID)" ]; then
  ENVSET OPENBAO_ROLE_ID "$(BAO read -field=role_id auth/approle/role/switchyard-controller/role-id)"
  ENVSET OPENBAO_SECRET_ID "$(BAO write -f -field=secret_id auth/approle/role/switchyard-controller/secret-id)"
fi
if [ ! -s secrets/dp_password ]; then
  printf '%s' "$(rand)" > secrets/dp_password
  BAO kv put kv/se/dataplane value="switchyard:$(cat secrets/dp_password)" >/dev/null
fi

say "certificates and the bundle signing key"
issue() { # name common_name ip_sans alt_names
  BAO write -format=json pki/issue/internal common_name="$2" ip_sans="$3" alt_names="$4" ttl=168h > "secrets/$1.json"
  PY "$1" <<'PYEOF'
import json, sys
name = sys.argv[1]
d = json.load(open(f"/secrets/{name}.json"))["data"]
open(f"/secrets/{name}.crt", "w").write(d["certificate"] + "\n" + d["issuing_ca"] + "\n")
open(f"/secrets/{name}.key", "w").write(d["private_key"] + "\n")
PYEOF
}
case "$HOST" in *[!0-9.]*) SANIP=127.0.0.1; SANDNS=$HOST ;; *) SANIP="$HOST,127.0.0.1"; SANDNS=localhost ;; esac
[ -s secrets/server.crt ] || issue server "$HOST" "$SANIP" "$SANDNS"
[ -s secrets/client.crt ] || issue client switchyard-controller "" ""
[ -s secrets/bundle.key ] || PY <<'PYEOF'
from cryptography.hazmat.primitives import serialization as s
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
k = Ed25519PrivateKey.generate()
open("/secrets/bundle.key", "wb").write(k.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption()))
PYEOF
chmod 644 secrets/*.crt secrets/ca.pem secrets/*.key secrets/bundle.key   # read by the controller container (uid 10001)

say "controller, UI and TLS front"
dc up -d --build >/dev/null
n=0; until [ "$(curl -sk -o /dev/null -w '%{http_code}' "https://127.0.0.1:$(ENVGET HTTPS_PORT | sed 's/^$/8443/')/api/v1/healthz")" = 200 ]; do
  n=$((n+1)); [ $n -lt 60 ] || { dc logs --tail 30 api; echo "the API did not come up" >&2; exit 1; }; sleep 3; done

say "first login"
if [ ! -s secrets/admin-token ]; then
  dc run --rm --no-deps migrate token --name quickstart 2>&1 | grep -o 'sy_[A-Za-z0-9_-]*' | tail -1 > secrets/admin-token
fi
PORT=$(ENVGET HTTPS_PORT); PORT=${PORT:-8443}
API="https://127.0.0.1:$PORT/api/v1"
CURL() { curl -sS --ssl-no-revoke --cacert secrets/ca.pem -H "Authorization: Bearer $(cat secrets/admin-token)" "$@"; }
if [ ! -s secrets/breakglass.txt ]; then
  # The break-glass administrator signs in with a password and a TOTP code; enroll it so the UI works at once.
  CURL -X DELETE "$API/system/break-glass/admin" >/dev/null 2>&1 || true
  CURL -X POST "$API/system/break-glass/admin" > secrets/bg.json
  PY <<'END_PY'
import base64, hashlib, hmac, json, struct, time
d = json.load(open("/secrets/bg.json"))
key = base64.b32decode(d["totp_secret"] + "=" * ((8 - len(d["totp_secret"]) % 8) % 8))
h = hmac.new(key, struct.pack(">Q", int(time.time() // 30)), hashlib.sha1).digest()
o = h[-1] & 15
code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
open("/secrets/bg.code", "w").write(f"{code:06d}")
open("/secrets/breakglass.txt", "w").write(
    "user: %s\npassword: %s\nTOTP secret (add to an authenticator app): %s\n"
    % (d["username"], d["password"], d["totp_secret"])
)
END_PY
  CURL -X POST -H 'Content-Type: application/json' -d "{\"totp\": \"$(cat secrets/bg.code)\"}" "$API/system/break-glass/admin/confirm" >/dev/null
  chmod 600 secrets/breakglass.txt; rm -f secrets/bg.json secrets/bg.code
fi
echo
echo "Switchyard is up:  https://$HOST:$PORT/   (the certificate is signed by secrets/ca.pem)"
echo "Admin API token:   $(cat secrets/admin-token)"
echo "                   syctl api login --url https://$HOST:$PORT --token <token>"
echo "CA for engines:    $(pwd)/secrets/ca.pem"
echo "Browser sign-in (break-glass): user, password and TOTP secret are in secrets/breakglass.txt"
echo "OpenBao root token and unseal key are in secrets/ (test instance only: see docs/production.md)."
echo "Next: docs/quickstart.md, step \"Sign in\"."
