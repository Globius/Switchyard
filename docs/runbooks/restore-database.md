# Restore the database (pgBackRest + Patroni)

Rehearsed by `deploy/prod/lab-drill.sh` on the lab cluster: restore to a point in time, then rebuild the
replicas.

**Before you start.** SEs keep serving without the controller. Decide the target: the latest state (omit
`--type` and `--target`) or a time just before the mistake (`--type=time --target="YYYY-MM-DD HH:MM:SS+00"`,
UTC). Check a backup covers it: `docker compose exec patroni pgbackrest --stanza=switchyard info`.
If only one node is broken and the others are healthy, do **not** restore: use `patronictl reinit` (step 6).

1. Stop the API and workers on all three nodes so nothing writes: `docker compose stop api worker`.
2. Stop Patroni on all three nodes: `docker compose stop patroni` (leave etcd running).
3. Clear the cluster state from etcd, from any node: `etcdctl del /switchyard/switchyard/ --prefix` (the
   Patroni `namespace` + `scope` key from `patroni.yml`).
4. Give every database node an **empty** data volume (a fresh VM, or remove the Patroni data volume after
   you snapshot it). Patroni will not restore over existing data.
5. On one node, start Patroni with the target in its environment:
   `RESTORE_TARGET_TIME="2026-10-11 04:38:04+00" docker compose up -d patroni`. The entrypoint bootstraps
   from `pgbackrest restore --type=time --target-action=promote` instead of `initdb`, replays WAL to the
   target and promotes. Check `patronictl -c /etc/patroni.yml list` shows it as leader. Then clear the
   variable in `node.env` so a later restart does not try to restore again.
6. Start Patroni on the other two nodes with empty volumes; each builds itself from the leader (pgBackRest,
   then `basebackup`). A node that kept old data must be rebuilt with
   `patronictl -c /etc/patroni.yml reinit switchyard <member> --force`.
7. Take a new full backup straight away: `pgbackrest --stanza=switchyard --type=full backup`.
8. Start the API and workers: `docker compose up -d api worker`. The `migrate` step is idempotent.

**Confirm.** `GET /api/v1/healthz` answers, the UI lists the tenants, and `syctl render` for one SE group matches
what the running SE reports (no drift). Changes made after the target time are gone: tell the users.

**Back out.** Snapshot the VM or copy the Patroni data volume before step 4; that is the only way back.
