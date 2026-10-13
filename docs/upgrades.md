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


## Configuration backups (built)

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
| `sftp` | SSH key login with the host key pinned by SHA-256 fingerprint (`ssh-keygen -lf` or `ssh-keyscan`); no trust on first use. The private key is a `secret_ref`. |

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
warns when there is no backup or the latest is more than seven days old.

**Restore** (`POST /system/backups/restore`, with `dry_run` to check first) loads every tenant of an archive into a controller that holds none of them; it
refuses with `409` and writes nothing if a tenant with the same id or name exists. Restored tenants have no engines: redeploy or re-enroll
them. Users, tokens, identity providers and the audit log come from the database restore.


## Looking for newer releases (built)

Off by default. A system administrator points the controller at a **release feed**, a signed `releases.json` that lists versions,
release notes, which versions each can be installed over, and bundle download links with their SHA-256. It can be a GitLab or GitHub
release asset or raw file, any HTTPS URL, or a mirror inside your network.

```sh
curl -X PUT $URL/api/v1/system/releases -d '{"url":"https://git.example.com/switchyard/-/releases/permalink/latest/downloads/releases.json",
  "channel":"stable","enabled":true,"trusted_keys":{"mykey":"<base64 Ed25519 public key>"}}'
curl -X POST $URL/api/v1/system/releases/check     # look now; otherwise once a day
```

- One HTTPS GET, no redirects, no cookies, nothing about your installation in the request. Responses over 1 MiB are refused.
- The feed is used **only if its Ed25519 signature verifies** against the project's keys or a key you list in `trusted_keys`. A hijacked repository or
  mirror cannot make the controller recommend a malicious release. A bad or missing signature, a failed fetch or an invalid document is
  shown as `error`; the last good feed stays in use and the controller is not affected.
- When a newer release is listed on your channel (`stable`, or `candidate` as well), `GET /system/upgrade/check` returns an `update_available` finding
  (a warning, never a block), the release notes (new, deprecated, breaking, action required) and the install path. If you are more than one
  release behind, the path lists each version to go through, from each release's `upgrade_from`.
- The feed never causes a download or an upgrade by itself.

**Release process.** Each release publishes `releases.json` signed with the project key (`switchyard.domain.releases.sign_feed`): the
signature is Ed25519 over the canonical JSON of `payload`. The project's public key goes into `PROJECT_KEYS`
(`controller/switchyard/services/release_feed.py`) once it exists; until then operators add the key themselves.

## Planned (ADR 0205)

Signed upgrade bundles, a dry-run plan, the
engine roll as a job with a health gate and pause, and the Upgrades page in the UI.
