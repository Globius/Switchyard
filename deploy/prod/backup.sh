#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Takes a pgBackRest backup, but only on the node that is the PostgreSQL primary right now (the primary moves, so run this
# on all three nodes from cron or a systemd timer; the two standbys do nothing). Usage, from deploy/prod:
#   sh backup.sh full      # a full backup, for example every Sunday
#   sh backup.sh diff      # a differential backup, for example every other night
# The first backup of a new cluster must be a full one. Example crontab on every node:
#   30 2 * * 0   cd /opt/switchyard/deploy/prod && sh backup.sh full
#   30 2 * * 1-6 cd /opt/switchyard/deploy/prod && sh backup.sh diff
set -eu
cd "$(dirname "$0")"
TYPE=${1:?usage: sh backup.sh full|diff}
case "$TYPE" in full|diff|incr) ;; *) echo "type must be full, diff or incr" >&2; exit 2 ;; esac
exec docker compose --env-file node.env exec -T patroni sh -c '
  [ "$(psql -U postgres -h /var/run/postgresql -Atc "select pg_is_in_recovery()")" = f ] || { echo "not the primary: skipped"; exit 0; }
  pgbackrest --stanza=switchyard --type='"$TYPE"' backup && pgbackrest --stanza=switchyard info | head -12'
