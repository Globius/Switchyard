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


## Upgrade bundles (built; apply is new and not yet lab-tested)

A bundle is one tar file holding a signed manifest and the release's files: controller and proxy images (`docker save`), the Helm chart, the
Compose files, the `sy-agent` binary and the SE template. The manifest lists every file with its SHA-256; one Ed25519 signature covers
the manifest and so the whole archive. Verifying extracts nothing and refuses links, extra members, a changed byte or an unknown key.

```sh
syctl release keygen release.pem --key-id release         # once; keep release.pem offline, publish the printed --trust value
syctl release build switchyard-0.2.0.sybundle --key release.pem --version 0.2.0 \
  --controller-image controller.tar --proxy-image proxy.tar --helm-chart switchyard-0.2.0.tgz \
  --compose compose.yaml --agent sy-agent --se-template se-template.tar \
  --upgrade-from 0.1.0 --notes notes.json                  # notes.json: {"new":[],"breaking":[],"action_required":[]}
syctl release verify switchyard-0.2.0.sybundle --trust release=<base64 public key>
syctl upgrade plan  switchyard-0.2.0.sybundle --trust release=<...>   # dry run against the running controller
```

Air-gapped sites need nothing but the file and the public key.

### Applying a bundle to a Compose node

```sh
syctl upgrade apply switchyard-0.2.0.sybundle --trust release=<key> \
  --compose-file deploy/prod/compose.yaml --env-file node.env --backup-target nightly
```

Run it on **one controller node at a time**; that is the canary. It verifies the bundle, writes a pre-upgrade configuration backup (or
needs `--no-backup`), loads the images, points `CONTROLLER_IMAGE` and `PROXY_IMAGE` in the env file at them, runs the migration once, recreates
`api`, `worker` and `proxy`, and waits for `/api/v1/healthz`. If the node does not come back healthy, the env file gets its old image names back and
the services are recreated on the old images, so a node is never left half-way. A failed migration changes nothing: migrations are additive by
rule (ADR 0205), so the old code keeps working on whatever the migration did. If a node is bad, leave the others on the old version, which is the
rollback. Not yet run on a real node: rehearse it in the lab first. Helm sites use `helm upgrade` with the chart from the bundle
([deploy-kubernetes.md](deploy-kubernetes.md)); the migration Job and `wait-schema` init container do the same ordering.



### Keeping a bundle on the controller (upload, check, download)

Admin > Upgrades > **Upgrade bundles** (system administrators) takes a bundle you upload, or downloads the one the release feed lists, and keeps it
**only after** its signature and every file's SHA-256 verify (against the project keys and the `trusted_keys` of the feed settings). It then shows
what applying it involves for the version this controller runs (steps, cautions, release notes) and the `syctl upgrade apply` command to run on each
controller node, and lets you download the verified file to take to another node or site. API: `PUT /system/bundles` (raw body),
`POST /system/bundles/fetch`, `GET /system/bundles`, `GET /system/bundles/{bundle}` (the plan), `.../file`, `DELETE`.

- **The controller does not apply bundles.** Replacing the container that runs the API needs the Docker socket or a privileged helper, which the
  component that holds every tenant's configuration should not have. Apply is `syctl upgrade apply` on each node, one at a time.
- **Where the file lives.** `SWITCHYARD_STAGING_DIR` (default `/var/lib/switchyard/staging`) on the node or pod that received the upload. The Compose
  file mounts a volume there; on Kubernetes set `storage.existingClaim` to a ReadWriteMany claim, otherwise each API pod has its own temporary copy.
  With several controller nodes, upload to the node you will apply from, or download the file and use it on each.
- **Large files.** The proxy in front allows uploads of up to 4 GiB on this one path (`SWITCHYARD_MAX_BUNDLE_BYTES` sets the controller's own limit).
  An Ingress needs the same: for ingress-nginx, `nginx.ingress.kubernetes.io/proxy-body-size: 4g` and `proxy-request-buffering: "off"` on the Ingress.
- Refused bundles (unknown key, changed byte, extra file, a link inside the archive) are answered `409` and leave nothing behind.

### Release feed: where the signature comes from

Maintainers sign releases with the project key; CI builds, signs and publishes them ([runbooks/release.md](runbooks/release.md), ADR 0206): `syctl release keygen`, `build`, `feed`. Controllers trust the
keys in `PROJECT_KEYS` (and any `trusted_keys` an operator adds); until the project key is published, set your own.

### Channels: stable, candidate, nightly

Every release in a feed has a channel. **stable** is a plain version (`0.2.0`), **candidate** a release candidate (`0.2.0-rc1`), **nightly** a build of
the day (`0.2.0-nightly.20261014`). The channel you choose in Admin > Upgrades also includes the narrower ones: candidate sees stable and
candidate, nightly sees all three. Versions order by semantic-version precedence, so `0.2.0-nightly.…` < `0.2.0-rc1` < `0.2.0`, and a
newer nightly replaces an older one.

To follow the nightlies on a test system:

1. Set the feed address to the nightly feed (`nightly.json`; for GitLab/GitHub see [runbooks/release.md](runbooks/release.md)) and the channel to **nightly**.
2. Add the nightly key under **Extra trusted signing keys** (`id=public key`, published with the releases). The nightly key is separate from the
   release key and not trusted by default, so a leaked nightly key cannot affect production sites.
3. Check now. Bundles install the same way as releases, with the same checks.

Nightlies are not releases: they carry no release notes, they can break things, and the project makes no promise to upgrade between them.

### Unsigned bundles (development only)

A bundle needs a signature from a trusted key everywhere it matters (Admin > Upgrades staging, `syctl upgrade plan` and `apply`). For a development
build without one: `syctl release build --unsigned …`, then `syctl upgrade apply BUNDLE … --allow-unsigned --confirm-unsigned <version>` (type the
version back). The file hashes are still checked, so a damaged download is caught, but nobody vouches for who made the bundle. The controller's own
staging and the UI never accept unsigned bundles.
## Engine roll (built, with fake actions only; lab run pending)

`POST /tenants/{t}/se-groups/{group}/rolls {"template_vmid": 9770}` replaces every enabled engine of the group with one cloned from a new
template, **one engine at a time**. For each engine: deploy a staged replacement, enroll it (the guest agent writes the enrollment files), enable it,
reapply the group, wait until the group's health score is 80 or more and the new engine passes a smoke check, retire the old engine, wait for the
score again, then the next engine.

- A step that fails or takes too long **pauses** the roll with the old and the new engine both in place. That pause is the canary and the rollback
  point: `POST /tenants/{t}/rolls/{roll} {"action":"resume"}` repeats the step, `"abort"` ends the roll (replaced engines stay replaced, the
  current old engine keeps serving), `"pause"` stops it on request. `GET /tenants/{t}/rolls` shows each engine's phase and the log.
- One roll per SE group at a time. Needs the group's `autoscale.deploy` and `autoscale.enroll` settings (autoscaling itself may be off) because that
  is how replacements are created and enrolled; the replacement must be a QEMU guest. LXC engines still use `deploy/lab/hosts/upgrade.sh`
  ([runbooks/rolling-upgrade.md](runbooks/rolling-upgrade.md)).
- **Schedule.** `window_start` / `window_end` (UTC `HH:MM`, across midnight when the end is earlier) and optional `window_days` (0 = Monday) let a new engine
  start only inside the window; `start_after` holds the whole roll until a moment; `soak_seconds` waits after each engine before the next. An engine already in
  progress is never interrupted when the window closes, so a window should be long enough for one engine (deploy, enroll, apply, health, retire). While it waits,
  the roll is `running` with `holding: true` and says why.
- Everything is audited (`roll.started`, `roll.pause`, `roll.resume`, `roll.abort`, `roll.paused`, `roll.done`).
- State is stored in the database and advanced every ten seconds by whichever worker runs the job, so a restart of the controller does not lose it.

## The Upgrades page

Admin > Upgrades (system administrators) shows the controller, database and engine versions, a one-line verdict ("Up to date", "Version X is available.
Upgrading is recommended.", or "Something needs fixing before an upgrade"), every finding with what to do about it, the release notes and install order when
the feed found a newer release, the recent backups with **Back up now**, and the feed settings with **Check now**. It advises and never blocks.
Engine rolls have their own page in each tenant (Infrastructure > Engine rolls): start one with a template, a window and a soak time, and pause, resume or abort it.

## Planned (ADR 0205)

One-click apply of a controller upgrade from the UI (needs an upgrade helper on each node and its own ADR), and a scheduled recurring check-and-notify.
