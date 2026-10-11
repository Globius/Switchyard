#!/bin/sh
# Build the Switchyard SE template contents on a Debian 13 host (run once as root, from cloud-init at
# template build or inside a container for tests). Idempotent. Variables:
#   HAPROXY_SERIES  HAProxy series from haproxy.debian.net (default 3.2)
#   AGENT_BIN       path to a built sy-agent binary (required)
#   DATAPLANE_BIN   path to the dataplaneapi binary (v3, from haproxytech releases)
set -eu
HAPROXY_SERIES="${HAPROXY_SERIES:-3.2}"
: "${AGENT_BIN:?set AGENT_BIN to the sy-agent binary}"
SRC="$(cd "$(dirname "$0")" && pwd)"
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq --no-install-recommends $(grep -v '^#' "$SRC/packages.txt")
curl -fsSL https://haproxy.debian.net/bernat.debian.org.gpg -o /usr/share/keyrings/haproxy.debian.net.gpg
echo "deb [signed-by=/usr/share/keyrings/haproxy.debian.net.gpg] http://haproxy.debian.net trixie-backports-${HAPROXY_SERIES} main" \
  > /etc/apt/sources.list.d/haproxy.list
apt-get update -qq
apt-get install -y -qq --no-install-recommends "haproxy=${HAPROXY_SERIES}.*"
[ -z "${DATAPLANE_BIN:-}" ] || install -m 0755 "$DATAPLANE_BIN" /usr/bin/dataplaneapi

install -m 0755 "$AGENT_BIN" /usr/local/bin/sy-agent
install -d -m 0755 /etc/switchyard /etc/switchyard/agent /etc/switchyard/haproxy /etc/switchyard/frr \
  /etc/switchyard/keepalived /run/switchyard
install -m 0644 "$SRC"/files/sy-agent.service "$SRC"/files/sy-agent-renew.service \
  "$SRC"/files/sy-agent-renew.timer "$SRC"/files/sy-agent-enroll.service \
  "$SRC"/files/dataplaneapi.service /etc/systemd/system/
install -m 0644 "$SRC"/files/dataplaneapi.yml /etc/switchyard/dataplaneapi.yml
install -m 0644 "$SRC"/files/haproxy-bootstrap.cfg /etc/switchyard/haproxy/haproxy.cfg
install -d /etc/switchyard/certs /etc/switchyard/haproxy/maps /etc/switchyard/haproxy/lists /var/lib/dataplaneapi/transactions
install -m 0644 "$SRC"/files/90-switchyard.conf /etc/sysctl.d/90-switchyard.conf
install -m 0644 "$SRC"/files/modules-switchyard.conf /etc/modules-load.d/switchyard.conf
install -d /etc/systemd/system/haproxy.service.d /etc/systemd/system/prometheus-node-exporter.service.d
install -m 0644 "$SRC"/files/haproxy-dropin.conf /etc/systemd/system/haproxy.service.d/switchyard.conf
# node_exporter on 9100 (SD default); HAProxy's own exporter (8405) is a rendered listener.
sed -i 's#^ARGS=.*#ARGS="--web.listen-address=:9100"#' /etc/default/prometheus-node-exporter

# The controller owns these; do not let packages start stock configs.
systemctl disable haproxy frr keepalived 2>/dev/null || true
systemctl enable dataplaneapi.service sy-agent-enroll.service sy-agent.service sy-agent-renew.timer prometheus-node-exporter \
  qemu-guest-agent chrony 2>/dev/null || true
# Generalize so clones get fresh identities.
rm -f /etc/machine-id /var/lib/dbus/machine-id; : > /etc/machine-id
cloud-init clean --logs 2>/dev/null || true
