#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Lints the chart and renders it with the value sets people use; fails on a render error or when an invariant breaks.
# Needs helm 3 or 4. Used by CI (job `helm`) and by hand: sh deploy/helm/test.sh
set -eu
cd "$(dirname "$0")/switchyard"
BASE="--set publicUrl=https://sy.example.net --set secrets.existingSecret=sy-secrets --set openbao.addr=https://bao.example.net:8200 --set openbao.existingSecret=sy-bao"
fail() { echo "FAIL: $*" >&2; exit 1; }
render() { helm template sy . -n sy $BASE "$@"; }

helm lint . $BASE --set tls.secretName=sy-tls >/dev/null

# 1. CloudNativePG, TLS front, NodePort.
out=$(render --set tls.secretName=sy-tls --set service.type=NodePort --set service.nodePort=30443)
echo "$out" | grep -q 'kind: Cluster' || fail "cnpg mode must render a Cluster"
echo "$out" | grep -q 'enableSuperuserAccess: true' || fail "the Cluster must expose the superuser secret"
echo "$out" | grep -q 'nodePort: 30443' || fail "nodePort missing"

# 2. External database, Ingress, no TLS front.
out=$(render --set database.mode=external --set database.external.host=pg.example.net \
  --set database.external.existingSecret=sy-db --set tls.enabled=false \
  --set ingress.enabled=true --set ingress.host=sy.example.net --set ingress.tlsSecretName=sy-ingress-tls)
echo "$out" | grep -q 'kind: Cluster' && fail "external mode must not render a Cluster"
echo "$out" | grep -q 'kind: Ingress' || fail "ingress missing"
echo "$out" | grep -q '@pg.example.net:5432/switchyard' || fail "database URL must use the external host"
echo "$out" | grep -q 'name: tls$' && fail "no TLS sidecar when tls.enabled is false"

# 3. Nothing secret is templated: no Secret object, no key material in the output.
render --set tls.secretName=sy-tls | grep -q '^kind: Secret' && fail "the chart must not create Secrets"
render --set tls.secretName=sy-tls | grep -q 'BEGIN .*PRIVATE KEY' && fail "key material in the chart output"

# 4. The worker has no HTTP probe and a pod-name identity; the API has probes on /api/v1/healthz.
out=$(render --set tls.secretName=sy-tls)
echo "$out" | grep -c 'path: /api/v1/healthz' | grep -q '^2$' || fail "the API needs a readiness and a liveness probe"
echo "$out" | grep -q 'args: \["worker", "--name", "$(POD_NAME)"\]' || fail "the worker name must be the pod name"

# 4b. Staged bundles and local backups: an emptyDir by default, an existing claim when given, and a big upload on the TLS front.out=$(render --set tls.secretName=sy-tls)echo "$out" | grep -q 'mountPath: /var/lib/switchyard' || fail "the API needs a writable /var/lib/switchyard"echo "$out" | grep -q 'location = /api/v1/system/bundles' || fail "the TLS front must allow large bundle uploads"out=$(render --set tls.secretName=sy-tls --set storage.existingClaim=sy-state)echo "$out" | grep -q 'claimName: sy-state' || fail "storage.existingClaim must be used"
# 5. Required values are enforced.
helm template sy . >/dev/null 2>&1 && fail "rendering without publicUrl must fail"
echo "helm chart checks passed"
