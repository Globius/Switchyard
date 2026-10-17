#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Build the Switchyard SE template contents on a Debian 13 host (run once as root, from cloud-init at
# template build or inside a container for tests). Idempotent. Variables:
#   HAPROXY_SERIES  HAProxy series from haproxy.debian.net (default 3.2)
#   AGENT_BIN       path to a built sy-agent binary (required)
#   DATAPLANE_BIN   path to the dataplaneapi binary (v3, from haproxytech releases)
#   AUTH_PROXY_BIN  path to the oauth2-proxy binary (the auth sidecar slot, ADR 0083); optional
#   CROWDSEC_SPOA_BIN path to the crowdsec-spoa-bouncer binary (the CrowdSec slot, ADR 0215); optional
#   CORAZA_SPOA_BIN path to the coraza-spoa binary (the WAF slot); CRS_TARBALL the Core Rule Set tarball; both optional
#   (a <file>.sha256 next to a binary, which fetch-binaries.sh writes, must match it)
# Versions come from versions.env next to this script.
set -eu
SRC="$(cd "$(dirname "$0")" && pwd)"
. "$SRC/versions.env"
HAPROXY_SERIES="${HAPROXY_SERIES:-3.2}"
: "${AGENT_BIN:?set AGENT_BIN to the sy-agent binary}"
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq --no-install-recommends $(grep -v '^#' "$SRC/packages.txt")
curl -fsSL https://haproxy.debian.net/bernat.debian.org.gpg -o /usr/share/keyrings/haproxy.debian.net.gpg
echo "deb [signed-by=/usr/share/keyrings/haproxy.debian.net.gpg] http://haproxy.debian.net trixie-backports-${HAPROXY_SERIES} main" \
  > /etc/apt/sources.list.d/haproxy.list
curl -fsSL https://keys.datadoghq.com/DATADOG_APT_KEY_CURRENT.public | gpg --dearmor -o /usr/share/keyrings/datadog.gpg
echo "deb [signed-by=/usr/share/keyrings/datadog.gpg] https://apt.vector.dev/ stable vector-0" > /etc/apt/sources.list.d/vector.list
apt-get update -qq
apt-get install -y -qq --no-install-recommends "haproxy=${HAPROXY_SERIES}.*" vector
[ -z "${DATAPLANE_BIN:-}" ] || install -m 0755 "$DATAPLANE_BIN" /usr/bin/dataplaneapi

install -m 0755 "$AGENT_BIN" /usr/local/bin/sy-agent
# Sidecar slots (ADR 0083): the units and users always exist and stay idle (each needs configuration that only the agent
# writes), the binaries are installed when supplied, so a template built without them can be given them later. Binaries
# listed with a <file>.sha256 next to them must match it.
verified() { # path: succeeds when there is no checksum file or it matches
  [ ! -f "$1.sha256" ] || (cd "$(dirname "$1")" && sha256sum -c "$(basename "$1").sha256" >/dev/null)
}
id sy-auth >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin sy-auth
id coraza >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin coraza
id crowdsec-spoa >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin crowdsec-spoa
install -d -m 0750 -g sy-auth /etc/switchyard/auth
install -d -m 0750 -g coraza /etc/switchyard/coraza
install -d -m 0750 -g crowdsec-spoa /etc/switchyard/crowdsec
install -m 0755 "$SRC"/files/sy-auth-run /usr/local/sbin/sy-auth-run
if [ -n "${AUTH_PROXY_BIN:-}" ]; then
  verified "$AUTH_PROXY_BIN" || { echo "oauth2-proxy does not match its recorded checksum" >&2; exit 1; }
  install -m 0755 "$AUTH_PROXY_BIN" /usr/local/bin/oauth2-proxy
fi
if [ -n "${CROWDSEC_SPOA_BIN:-}" ]; then
  verified "$CROWDSEC_SPOA_BIN" || { echo "crowdsec-spoa-bouncer does not match its recorded checksum" >&2; exit 1; }
  install -m 0755 "$CROWDSEC_SPOA_BIN" /usr/local/bin/crowdsec-spoa-bouncer
fi
if [ -n "${CORAZA_SPOA_BIN:-}" ]; then
  verified "$CORAZA_SPOA_BIN" || { echo "coraza-spoa does not match its recorded checksum" >&2; exit 1; }
  install -m 0755 "$CORAZA_SPOA_BIN" /usr/local/bin/coraza-spoa
fi
if [ -n "${CRS_TARBALL:-}" ]; then   # the OWASP Core Rule Set, version from versions.env
  echo "$CRS_SHA256  $CRS_TARBALL" | sha256sum -c - >/dev/null || { echo "Core Rule Set does not match its pinned checksum" >&2; exit 1; }
  install -d /usr/share/coreruleset
  rm -rf "/usr/share/coreruleset/$CRS_VERSION"
  mkdir "/usr/share/coreruleset/$CRS_VERSION"
  tar -xzf "$CRS_TARBALL" -C "/usr/share/coreruleset/$CRS_VERSION" --strip-components=1
  ln -sfn "/usr/share/coreruleset/$CRS_VERSION" /usr/share/coreruleset/current
fi
install -m 0644 "$SRC"/files/sy-auth.service "$SRC"/files/coraza-spoa.service "$SRC"/files/crowdsec-spoa-bouncer.service /etc/systemd/system/
install -d -m 0755 /etc/switchyard /etc/switchyard/agent /etc/switchyard/haproxy /etc/switchyard/frr \
  /etc/switchyard/keepalived /run/switchyard
install -m 0644 "$SRC"/files/sy-agent.service "$SRC"/files/sy-agent-renew.service \
  "$SRC"/files/sy-agent-renew.timer "$SRC"/files/sy-agent-enroll.service \
  "$SRC"/files/dataplaneapi.service /etc/systemd/system/
install -m 0644 "$SRC"/files/dataplaneapi.yml.tmpl /etc/switchyard/dataplaneapi.yml.tmpl
install -m 0755 "$SRC"/files/sy-render-dataplane /usr/local/sbin/sy-render-dataplane
install -m 0644 "$SRC"/files/haproxy-bootstrap.cfg /etc/switchyard/haproxy/haproxy.cfg
install -d /etc/switchyard/certs /etc/switchyard/haproxy/maps /etc/switchyard/haproxy/lists /var/lib/dataplaneapi/transactions
install -m 0644 "$SRC"/files/90-switchyard.conf /etc/sysctl.d/90-switchyard.conf
install -m 0644 "$SRC"/files/modules-switchyard.conf /etc/modules-load.d/switchyard.conf
install -m 0644 "$SRC"/files/nftables.conf /etc/nftables.conf
install -d /etc/switchyard/nftables
install -d /etc/systemd/system/haproxy.service.d /etc/systemd/system/prometheus-node-exporter.service.d
install -m 0644 "$SRC"/files/haproxy-dropin.conf /etc/systemd/system/haproxy.service.d/switchyard.conf
# Keepalived and FRR read their configuration from /etc/switchyard (the only place the agent may write).
id keepalived_script >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin keepalived_script
install -d /etc/systemd/system/keepalived.service.d
install -m 0644 "$SRC"/files/keepalived-dropin.conf /etc/systemd/system/keepalived.service.d/switchyard.conf
install -m 0644 "$SRC"/files/tmpfiles-switchyard.conf /etc/tmpfiles.d/switchyard.conf
install -d /etc/systemd/system/frr.service.d
install -m 0644 "$SRC"/files/frr-dropin.conf /etc/systemd/system/frr.service.d/switchyard.conf
ln -sf /etc/switchyard/frr/frr.conf /etc/frr/frr.conf
sed -i 's/^bgpd=no/bgpd=yes/' /etc/frr/daemons
sed -i 's/^bfdd=no/bfdd=yes/' /etc/frr/daemons
install -m 0644 "$SRC"/files/vector.yaml /etc/switchyard/vector.yaml
install -d -m 0755 /etc/switchyard/vector
install -m 0755 "$SRC"/files/sy-vector-sinks /usr/local/sbin/sy-vector-sinks
install -d /etc/systemd/system/vector.service.d
install -m 0644 "$SRC"/files/vector-dropin.conf /etc/systemd/system/vector.service.d/switchyard.conf
# node_exporter on 9100 (SD default); HAProxy's own exporter (8405) is a rendered listener.
sed -i 's#^ARGS=.*#ARGS="--web.listen-address=:9100"#' /etc/default/prometheus-node-exporter

# The controller owns these; do not let packages start stock configs.
# HAProxy runs the bootstrap config until the first apply (the Data Plane API reloads it). Keepalived and FRR are
# enabled too, but each skips itself until the controller's config file exists (drop-ins), so an SE that reboots
# comes back with its VIP and BGP sessions without the controller (SEs must survive a controller outage).
systemctl enable haproxy frr keepalived nftables sy-auth coraza-spoa crowdsec-spoa-bouncer 2>/dev/null || true
systemctl enable vector dataplaneapi.service sy-agent-enroll.service sy-agent.service sy-agent-renew.timer prometheus-node-exporter \
  qemu-guest-agent chrony 2>/dev/null || true
# Generalize so clones get fresh identities.
rm -f /etc/machine-id /var/lib/dbus/machine-id; : > /etc/machine-id
cloud-init clean --logs 2>/dev/null || true
