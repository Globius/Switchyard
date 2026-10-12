#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Prepares a TEST install of the Switchyard chart in one namespace: a single OpenBao node (initialised, unsealed, with
# the agent PKI, the controller policy and an AppRole) and the Secrets the chart expects. The keys it prints go to
# standard output only once; keep the output (docs/kubernetes-deploy.md). Production uses its own OpenBao and secret
# manager: this script is not for it.
#   sh bootstrap-test.sh --namespace NS --host ADDRESS[,ADDRESS...] [--image controller-image]
set -eu
NS=switchyard; HOSTS=""; IMG=switchyard-controller:latest; K=${KUBECTL:-kubectl}
while [ $# -gt 0 ]; do case "$1" in --namespace) NS=$2; shift 2 ;; --host) HOSTS=$2; shift 2 ;; --image) IMG=$2; shift 2 ;; *) echo "usage: sh bootstrap-test.sh --namespace NS --host ADDRESS[,ADDRESS...] [--image IMAGE]" >&2; exit 2 ;; esac; done
[ -n "$HOSTS" ] || { echo "--host is required (how browsers and engines reach the controller)" >&2; exit 2; }
cd "$(dirname "$0")"
say() { echo "== $*" >&2; }
rand() { openssl rand -hex 16; }
kn() { $K -n "$NS" "$@"; }
BAO() { kn exec openbao-0 -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$ROOT" bao "$@"; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

$K get ns "$NS" >/dev/null 2>&1 || $K create ns "$NS" >/dev/null
say "OpenBao"
kn apply -f examples/openbao-test.yaml >/dev/null
until kn get pod openbao-0 >/dev/null 2>&1; do sleep 2; done
kn wait --for=condition=Ready pod/openbao-0 --timeout=300s >/dev/null
ST() { kn exec openbao-0 -- env BAO_ADDR=http://127.0.0.1:8200 bao status 2>/dev/null || true; }
n=0; until ST | grep -q "Initialized"; do n=$((n+1)); [ $n -lt 40 ] || { echo "OpenBao does not answer" >&2; exit 1; }; sleep 3; done
if ! ST | grep -q 'Initialized *true'; then
  kn exec openbao-0 -- env BAO_ADDR=http://127.0.0.1:8200 bao operator init -key-shares=1 -key-threshold=1 -format=json | tr -d '\n ' > "$T/init.json"
  UNSEAL=$(sed -n 's/.*"unseal_keys_b64":\["\([^"]*\)".*/\1/p' "$T/init.json")
  ROOT=$(sed -n 's/.*"root_token":"\([^"]*\)".*/\1/p' "$T/init.json")
  [ -n "$UNSEAL" ] && [ -n "$ROOT" ] || { echo "OpenBao init failed" >&2; exit 1; }
  kn create secret generic openbao-test-keys --from-literal=unseal-key="$UNSEAL" --from-literal=root-token="$ROOT" >/dev/null
fi
UNSEAL=$(kn get secret openbao-test-keys -o jsonpath='{.data.unseal-key}' | base64 -d)
ROOT=$(kn get secret openbao-test-keys -o jsonpath='{.data.root-token}' | base64 -d)
n=0; until ST | grep -q "Sealed *false"; do
  kn exec openbao-0 -- env BAO_ADDR=http://127.0.0.1:8200 bao operator unseal "$UNSEAL" >/dev/null 2>&1 || true
  n=$((n+1)); [ $n -lt 40 ] || { echo "OpenBao stays sealed" >&2; exit 1; }; sleep 3
done

say "PKI, policy and AppRole"
if ! BAO secrets list 2>/dev/null | grep -q '^pki/'; then
  BAO secrets enable -path=kv -version=2 kv >/dev/null 2>&1 || true
  BAO secrets enable -path=pki pki >/dev/null
  BAO secrets tune -max-lease-ttl=87600h pki >/dev/null
  BAO write -field=certificate pki/root/generate/internal common_name="Switchyard test CA" ttl=87600h > "$T/ca.pem"
  echo >> "$T/ca.pem"
  BAO write pki/roles/agents allow_any_name=true allow_ip_sans=true enforce_hostnames=false key_type=any \
    use_csr_sans=false use_csr_common_name=false max_ttl=168h ttl=24h >/dev/null
  BAO write pki/roles/internal allow_any_name=true allow_ip_sans=true enforce_hostnames=false key_type=ec key_bits=256 \
    max_ttl=168h ttl=24h >/dev/null
  $K -n "$NS" run policy --rm -i --restart=Never --quiet --image="$IMG" --image-pull-policy=IfNotPresent \
    --command -- syctl openbao-policy --shared kv/se/ > "$T/policy.hcl"
  printf '\npath "pki/sign/agents" { capabilities = ["update"] }\n' >> "$T/policy.hcl"
  kn exec -i openbao-0 -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$ROOT" bao policy write switchyard-controller - < "$T/policy.hcl" >/dev/null
  BAO auth enable approle >/dev/null 2>&1 || true
  BAO write auth/approle/role/switchyard-controller token_policies=switchyard-controller >/dev/null
  ROLE=$(BAO read -field=role_id auth/approle/role/switchyard-controller/role-id)
  SECRET=$(BAO write -f -field=secret_id auth/approle/role/switchyard-controller/secret-id)
  kn create secret generic switchyard-openbao --from-literal=role-id="$ROLE" --from-literal=secret-id="$SECRET" >/dev/null
  BAO kv put kv/se/dataplane value="switchyard:$(rand)" >/dev/null

  say "certificates and the bundle signing key"
  SANIP=127.0.0.1; SANDNS=localhost
  for h in $(echo "$HOSTS" | tr ',' ' '); do
    case "$h" in *:*) SANIP="$SANIP,$h" ;; *[!0-9.]*) SANDNS="$SANDNS,$h" ;; *) SANIP="$SANIP,$h" ;; esac
  done
  FIRST=${HOSTS%%,*}
  unesc() { printf '%b
' "$(cat)"; }   # the JSON holds the PEM with escaped newlines
  issue() { # cn ip_sans alt_names prefix
    BAO write -format=json pki/issue/internal common_name="$1" ip_sans="$2" alt_names="$3" ttl=168h | tr -d '\n' > "$T/$4.json"
    sed -n 's/.*"certificate": *"\([^"]*\)".*/\1/p' "$T/$4.json" | unesc > "$T/$4.crt"
    sed -n 's/.*"issuing_ca": *"\([^"]*\)".*/\1/p' "$T/$4.json" | unesc >> "$T/$4.crt"
    sed -n 's/.*"private_key": *"\([^"]*\)".*/\1/p' "$T/$4.json" | unesc > "$T/$4.key"
  }
  issue "$FIRST" "$SANIP" "$SANDNS" server
  issue switchyard-controller "" "" client
  openssl genpkey -algorithm ed25519 -out "$T/bundle.key" 2>/dev/null
  kn create secret generic switchyard-secrets --from-file=bundle.key="$T/bundle.key" --from-file=ca.pem="$T/ca.pem" \
    --from-file=client.crt="$T/client.crt" --from-file=client.key="$T/client.key" --from-literal=metrics-token="$(rand)" >/dev/null
  kn create secret tls switchyard-tls --cert="$T/server.crt" --key="$T/server.key" >/dev/null
  kn create secret generic switchyard-ca --from-file=ca.pem="$T/ca.pem" >/dev/null
fi
echo "Secrets in namespace $NS: switchyard-secrets, switchyard-openbao, switchyard-tls, switchyard-ca (engines trust ca.pem)." >&2
echo "OpenBao test keys: secret openbao-test-keys (unseal key and root token). Test instance only." >&2
