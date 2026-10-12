#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Starts Patroni. With RESTORE_TARGET_TIME set (and an empty data directory) the node bootstraps by a
# pgBackRest point-in-time restore instead of initdb; set RESTORE_TARGET_TIME="" afterwards. See
# docs/runbooks/restore-database.md.
set -eu
mkdir -p /data/pgdata && chmod 700 /data/pgdata
CONFIG=/etc/patroni.yml
if [ -n "${RESTORE_TARGET_TIME:-}" ]; then
  CONFIG=/tmp/patroni-restore.yml
  RESTORE_TARGET_TIME="$RESTORE_TARGET_TIME" python - <<'PY'
import os, yaml
t = os.environ["RESTORE_TARGET_TIME"]
cfg = yaml.safe_load(open("/etc/patroni.yml"))
cfg["bootstrap"]["method"] = "pgbackrest_restore"
cfg["bootstrap"].pop("post_bootstrap", None)  # the restored cluster already has its database and stanza
cfg["bootstrap"]["pgbackrest_restore"] = {
    "command": f'pgbackrest --stanza=switchyard --type=time "--target={t}" --target-action=promote restore',
    "no_params": True,
    "keep_existing_recovery_conf": True,
}
yaml.safe_dump(cfg, open("/tmp/patroni-restore.yml", "w"))
PY
fi
exec patroni "$CONFIG"
