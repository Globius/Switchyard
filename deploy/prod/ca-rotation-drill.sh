#!/bin/sh
# Rehearses docs/runbooks/rotate-ca.md against a dev OpenBao (container sy-openbao, token devroot).
# It uses its own mounts (pki-drill-old, pki-drill-new) and removes them at the end.
set -eu
BAO() { docker exec -e BAO_ADDR=http://127.0.0.1:8200 -e BAO_TOKEN=devroot sy-openbao bao "$@"; }
W=$(mktemp -d); trap 'BAO secrets disable pki-drill-old >/dev/null 2>&1 || true; BAO secrets disable pki-drill-new >/dev/null 2>&1 || true; rm -rf "$W"' EXIT
fail() { echo "FAIL: $*"; exit 1; }

mkca() { # mount name
  BAO secrets enable -path="$1" pki >/dev/null
  BAO secrets tune -max-lease-ttl=87600h "$1" >/dev/null
  BAO write -field=certificate "$1/root/generate/internal" common_name="$2" ttl=87600h > "$W/$1.ca.pem"; echo >> "$W/$1.ca.pem"
  BAO write "$1/roles/agent" allow_any_name=true max_ttl=168h >/dev/null
}
issue() { # mount host -> $W/<host>.pem
  BAO write -field=certificate "$1/issue/agent" common_name="$2" ttl=24h > "$W/$2.pem"; echo >> "$W/$2.pem"
}
ok()  { (cd "$W" && openssl verify -CAfile "$(basename "$1")" "$(basename "$2")") >/dev/null 2>&1; }  # relative names: Windows openssl

echo "== old CA issues se-1's certificate"
mkca pki-drill-old "Switchyard drill CA (old)"; issue pki-drill-old se-1.old
echo "== step 1-2: new CA created; controller trusts both"
mkca pki-drill-new "Switchyard drill CA (new)"
cat "$W/pki-drill-old.ca.pem" "$W/pki-drill-new.ca.pem" > "$W/bundle.pem"
ok "$W/bundle.pem" "$W/se-1.old.pem" || fail "bundle does not accept the old certificate"
echo "== step 3-4: se-2 and a renewed se-1 get certificates from the new CA; both CAs still trusted"
issue pki-drill-new se-2.new
ok "$W/bundle.pem" "$W/se-2.new.pem" || fail "bundle does not accept the new certificate"
ok "$W/pki-drill-old.ca.pem" "$W/se-2.new.pem" && fail "old CA alone must not accept new certificates"
echo "== step 5: old CA removed from the controller bundle"
cp "$W/pki-drill-new.ca.pem" "$W/bundle.pem"
ok "$W/bundle.pem" "$W/se-2.new.pem" || fail "new CA alone must accept new certificates"
ok "$W/bundle.pem" "$W/se-1.old.pem" && fail "an engine that never renewed must now be rejected"
echo "rotation drill passed: overlap accepted both, after removal only renewed engines are trusted"
