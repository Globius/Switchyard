#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Only operator-listed proxy addresses may supply the client address. Empty trusts no hop.
set -eu
out=${NGINX_ENVSUBST_OUTPUT_DIR:-/etc/nginx/conf.d}/00-trusted-proxies.conf
: > "$out"
for cidr in ${TRUSTED_PROXY_CIDRS:-}; do
    case "$cidr" in
        *[!0-9a-fA-F:./]*) echo "TRUSTED_PROXY_CIDRS must contain IP addresses or CIDRs" >&2; exit 1 ;;
    esac
    printf 'set_real_ip_from %s;\n' "$cidr" >> "$out"
done
