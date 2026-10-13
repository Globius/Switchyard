# Upgrades

How to see what you run and whether to upgrade. Design: [ADR 0205](adr/0205-upgrades-versions-bundles-backups.md). The step-by-step
procedure is still in [production.md](production.md#11-upgrades) and [runbooks/rolling-upgrade.md](runbooks/rolling-upgrade.md).

## Upgrade check (built)

`GET /api/v1/system/upgrade/check` (system administrator) compares:

- the **controller version** with the newest known release (the controller itself until a release feed exists);
- the **database revision** with the one this release expects: `current`, `pending` (run `syctl db upgrade` after a backup) or
  `newer` (a newer release wrote it; do not run this one against it);
- every engine's **agent version** (as last reported) with the compatibility table.

It only advises. An engine on an old agent keeps receiving configuration; the finding says which template to roll to.
Findings carry a `severity` (`info`, `warning`, `error`), a stable `code` (`agent_behind`, `agent_unsupported`,
`agent_version_unknown`, `schema_pending`, `schema_newer_than_code`), the subject and the action.


## Configuration backups (built, SFTP pending)

A backup is the desired state of every tenant (virtual services, pools, SE groups, certificates and so on, with `secret_ref`
values and never secret values), compressed and encrypted with AES-256-GCM. The key comes from a passphrase read from your secrets
backend (`passphrase_ref`). **Keep the passphrase somewhere other than the backup**: without it an archive cannot be opened.

What it is for: a safety copy before an upgrade, and moving the configuration to another cluster. What it is **not**: a database
backup. Users, tokens, identity-provider settings and the audit log come back only from pgBackRest
([restore-database](runbooks/restore-database.md)).

Destinations (`POST /system/backup-targets`), all with credentials as `secret_ref`s:

| Kind | Notes |
| --- | --- |
| `local` | A directory under `SWITCHYARD_BACKUP_DIR` (default `/var/lib/switchyard/backups`). Mount a volume there; a copy on the controller's own disk does not survive losing the controller. |
| `s3` | Any S3-compatible service (AWS, MinIO, Ceph, Backblaze B2), HTTPS only, path-style. |
| `ftp` | FTPS by default (certificate verified). Plain FTP is possible and sends the password in clear. |
| `sftp` | Accepted in settings, **not yet usable** (waiting on the choice of SSH library, ADR 0205). |

`keep` archives are retained per target; older ones are deleted after each successful backup. Only files named
`switchyard-<version>-<UTC time>.syb` are ever listed or deleted.

```sh
curl -X POST $URL/api/v1/system/backup-targets -d '{"name":"nightly","passphrase_ref":"secret/backup/pass",
  "target":{"kind":"s3","endpoint":"https://s3.example.com","bucket":"sy-backups",
  "access_key_ref":"secret/backup/ak","secret_key_ref":"secret/backup/sk"},"keep":14}'
curl -X POST $URL/api/v1/system/backup-targets/nightly/test        # lists archives; writes nothing
curl -X POST $URL/api/v1/system/backups -d '{"target":"nightly","reason":"pre-upgrade"}'
curl -X POST $URL/api/v1/system/backups/verify -d '{"target":"nightly","archive":"switchyard-0.1.0-20261013T040000Z.syb"}'
```

Every run, successful or failed, is recorded (`GET /system/backups`) and audited (`backup.created`, `backup.failed`). The upgrade check
warns when there is no backup or the latest is more than seven days old. Restore into an empty controller is next.

## Planned (ADR 0205)

Release feed and update check, signed upgrade bundles, restore from a backup, SFTP, a dry-run plan, the
engine roll as a job with a health gate and pause, and the Upgrades page in the UI.
