#!/bin/sh
# Assertions on a provisioned SE filesystem (run inside the test image).
set -eu
fail() { echo "FAIL: $*" >&2; exit 1; }
haproxy -v | head -1 | grep -q 'version 3\.2' || fail "haproxy is not 3.2"
for b in sy-agent; do [ -x /usr/local/bin/$b ] || fail "$b missing"; done
[ -x /usr/bin/dataplaneapi ] || fail "dataplaneapi missing"
for b in keepalived ipvsadm nft vtysh; do command -v $b >/dev/null || fail "$b missing"; done
keepalived --version 2>&1 | grep -q 'v2\.[3-9]' || fail "keepalived < 2.3"
[ -x /usr/bin/prometheus-node-exporter ] || fail "node exporter missing"
grep -q ':9100' /etc/default/prometheus-node-exporter || fail "node exporter port"
for u in sy-agent sy-agent-enroll sy-agent-renew dataplaneapi; do
  [ -f /etc/systemd/system/$u.service ] || fail "$u unit"
done
systemd-analyze verify /etc/systemd/system/sy-agent.service /etc/systemd/system/sy-agent-enroll.service \
  /etc/systemd/system/sy-agent-renew.service /etc/systemd/system/sy-agent-renew.timer 2>&1 | grep -v -E 'not executable|/usr/local/bin/sy-agent' || true
dpkg -s frr-pythontools >/dev/null 2>&1 || fail "frr-pythontools missing (systemctl reload frr needs it)"
command -v vector >/dev/null || fail "vector missing"
VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION=true LOKI_URL=http://loki:3100 TENANT_ID=t TENANT_NAME=t SE_GROUP=g ENGINE=e vector validate --no-environment /etc/switchyard/vector.yaml   || fail "vector config invalid"
# Things the first real hosts showed missing (ADR 0079): nftables enabled with a config that includes the managed files,
# the Vector drop-in replacing the packaged validate step, and the IPv6 sysctls the services need.
[ -f /etc/nftables.conf ] && grep -q 'include "/etc/switchyard/nftables/\*.nft"' /etc/nftables.conf || fail "nftables.conf"
ls /etc/systemd/system/*.wants/nftables.service >/dev/null 2>&1 || fail "nftables is not enabled"
grep -q '^ExecStartPre=$' /etc/systemd/system/vector.service.d/switchyard.conf || fail "vector drop-in keeps the packaged ExecStartPre"
for k in net.ipv6.ip_nonlocal_bind net.ipv6.conf.all.forwarding net.ipv6.conf.all.accept_ra; do
  grep -q "^$k" /etc/sysctl.d/90-switchyard.conf || fail "sysctl $k"
done
# The auth sidecar slot (ADR 0083): idle unit, its user and directory; the binary only when the image was given one.
id sy-auth >/dev/null 2>&1 || fail "user sy-auth"
[ -d /etc/switchyard/auth ] || fail "/etc/switchyard/auth"
[ -f /etc/systemd/system/sy-auth.service ] || fail "sy-auth unit"
grep -q 'ConditionPathExistsGlob=/etc/switchyard/auth/\*.cfg' /etc/systemd/system/sy-auth.service || fail "sy-auth must wait for its config"
[ -x /usr/local/sbin/sy-auth-run ] && bash -n /usr/local/sbin/sy-auth-run || fail "sy-auth-run"
# The WAF slot (block P10): user, unit, config directory, and the binary and pinned Core Rule Set when supplied.
id coraza >/dev/null 2>&1 || fail "user coraza"
[ -d /etc/switchyard/coraza ] || fail "/etc/switchyard/coraza"
grep -q 'ConditionPathExists=/etc/switchyard/coraza/config.yaml' /etc/systemd/system/coraza-spoa.service || fail "coraza-spoa must wait for its config"
systemd-analyze verify /etc/systemd/system/coraza-spoa.service 2>&1 | grep -v -E 'not executable|coraza-spoa|ConditionPathExists' || true
[ ! -x /usr/local/bin/coraza-spoa ] || /usr/local/bin/coraza-spoa --help >/dev/null 2>&1 || fail "coraza-spoa does not run"
if [ -e /usr/share/coreruleset/current ]; then
  [ -f /usr/share/coreruleset/current/crs-setup.conf.example ] && [ -d /usr/share/coreruleset/current/rules ] || fail "Core Rule Set"
fi
systemd-analyze verify /etc/systemd/system/sy-auth.service 2>&1 | grep -v -E 'not executable|oauth2-proxy|ConditionPathExists' || true
[ ! -x /usr/local/bin/oauth2-proxy ] || /usr/local/bin/oauth2-proxy --version | grep -q 'oauth2-proxy' || fail "oauth2-proxy does not run"
[ ! -s /etc/machine-id ] || fail "machine-id not cleared"
echo "se-image checks passed"
# Data Plane API answers over mTLS with the agent's certificate, and rejects clients without one.
cd /tmp
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=sy-test -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
  -keyout /etc/switchyard/agent/tls.key -out /etc/switchyard/agent/tls.crt 2>/dev/null
cp /etc/switchyard/agent/tls.crt /etc/switchyard/agent/ca.crt
printf 'CONTROLLER_URL=https://c
DP_USER=sy
DP_PASSWORD=s3cret
' > /etc/switchyard/agent/enroll.env
/usr/local/sbin/sy-render-dataplane
grep -q 'password: "s3cret"' /etc/switchyard/dataplaneapi.yml || fail "credentials not rendered"
mkdir -p /run/haproxy
haproxy -c -f /etc/switchyard/haproxy/haproxy.cfg >/dev/null || fail "bootstrap haproxy.cfg invalid"
haproxy -D -f /etc/switchyard/haproxy/haproxy.cfg -p /run/haproxy.pid
dataplaneapi -f /etc/switchyard/dataplaneapi.yml >/tmp/dp.log 2>&1 &
i=0; until curl -sk --cert /etc/switchyard/agent/tls.crt --key /etc/switchyard/agent/tls.key -u sy:s3cret https://127.0.0.1:5556/v3/info | grep -q api; do
  i=$((i+1)); [ $i -lt 20 ] || { cat /tmp/dp.log; fail "dataplaneapi did not answer over mTLS"; }; sleep 0.5; done
code=$(curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:5556/v3/info || true)
[ "$code" = 000 ] || [ "$code" = 401 ] || fail "dataplaneapi answered without a client certificate ($code)"
echo "dataplaneapi mTLS ok"
