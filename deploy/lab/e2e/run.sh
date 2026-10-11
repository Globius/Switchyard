#!/bin/sh
# End-to-end lab, whole run: base.sh (Proxmox, template, OpenBao, controller) then scenario.sh (engines, enrollment,
# traffic, failover, canary ordering, BGP). About 15 minutes. Then: sh deploy/lab/e2e/teardown.sh
set -eu
cd "$(dirname "$0")"
sh base.sh
sh scenario.sh
