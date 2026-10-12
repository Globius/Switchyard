#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# End-to-end lab, whole run: base.sh (Proxmox, template, OpenBao, controller) then scenario.sh (engines, enrollment,
# traffic, failover, canary ordering, BGP). About 15 minutes. Then: sh deploy/lab/e2e/teardown.sh
set -eu
cd "$(dirname "$0")"
sh base.sh
sh scenario.sh
[ "${GUEST_KIND:-lxc}" != lxc ] || sh unpriv.sh
