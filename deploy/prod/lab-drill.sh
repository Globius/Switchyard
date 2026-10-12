#!/bin/sh

# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

# Lab drill on the 3-node cluster: failover, then point-in-time restore. Run from deploy/prod:
#   sh lab-drill.sh            (takes about 5 minutes; leaves the lab running, `down -v` to clean up)
set -eu
D="docker compose -f compose.lab.yaml"
SQL="docker run --rm --network switchyard-lab_default -e PGPASSWORD=lab-superuser postgres:17 psql -h haproxy -U postgres -d switchyard -tA"
members() { $D exec -T "$1" patronictl -c /etc/patroni.yml list; }
leader() { $D exec -T db1 patronictl -c /etc/patroni.yml list | awk -F'|' '/Leader/ {gsub(/ /,"",$2); print $2}'; }
wait_leader() { n=0; until [ -n "$(leader 2>/dev/null)" ] && $SQL -c 'select 1' >/dev/null 2>&1; do n=$((n+1)); [ $n -lt 60 ] || { echo "no leader"; exit 1; }; sleep 3; done; }

$D down -v >/dev/null 2>&1 || true
$D up -d --build --wait >/dev/null
wait_leader
echo "== cluster up, leader $(leader)"

echo "== backup, then data before and after the restore target"
$SQL -c "create table drill(n int); insert into drill values (1),(2),(3)" >/dev/null
$D exec -T "$(leader)" pgbackrest --stanza=switchyard --type=full backup
$SQL -c "insert into drill values (4)" >/dev/null
$SQL -c "select pg_switch_wal()" >/dev/null
sleep 2
TARGET=$($SQL -c "select to_char(now() at time zone 'utc','YYYY-MM-DD HH24:MI:SS+00')")
sleep 2
$SQL -c "drop table drill" >/dev/null   # the mistake we restore from
$SQL -c "select pg_switch_wal()" >/dev/null
sleep 5

echo "== restore to $TARGET"
$D stop db1 db2 db3 >/dev/null
$D exec -T etcd1 etcdctl del /switchyard/switchyard/ --prefix >/dev/null
# A replacement node starts with an empty data volume (here: drop the lab volumes).
$D rm -f db1 db2 db3 >/dev/null
docker volume rm switchyard-lab_pgdata1 switchyard-lab_pgdata2 switchyard-lab_pgdata3 >/dev/null
RESTORE_TARGET_TIME="$TARGET" $D up -d --no-deps db1 >/dev/null
n=0; until $SQL -c "select 1" >/dev/null 2>&1; do n=$((n+1)); [ $n -lt 60 ] || { $D logs --tail 30 db1; exit 1; }; sleep 3; done
# A backup taken on the new timeline must exist before replicas are built, or they replay the
# abandoned timeline from the archive and cannot follow the new leader.
$D exec -T db1 pgbackrest --stanza=switchyard --type=full backup
$D up -d --no-deps db2 db3 >/dev/null
n=0; until [ "$(members db1 | grep -c streaming)" = 2 ]; do n=$((n+1)); [ $n -lt 90 ] || break; sleep 5; done
members db1
GOT=$($SQL -c "select string_agg(n::text,',' order by n) from drill")
[ "$GOT" = "1,2,3,4" ] || { echo "FAIL: expected 1,2,3,4 after restore, got '$GOT'"; exit 1; }
if [ "$(members db1 | grep -c streaming)" != 2 ]; then
  echo "FAIL: replicas did not come up"; $D logs --tail 40 db2; exit 1
fi
echo "restore drill passed: $GOT"
