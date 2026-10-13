# 0205. Upgrades: versions and compatibility, upgrade bundles, backups, checks, rollback

Status: Proposed
Date: 2026-10-13

## Context

Upgrades today are a hand procedure (`docs/production.md` section 11, `docs/runbooks/rolling-upgrade.md`, ADR 0083). Migrations
are forward-only and nothing says they must be compatible with the previous release. The agent reports `agent_version` but
the controller only displays it. There is no supported version matrix, no pre-upgrade backup of the configuration, no
packaged upgrade artifact and no upgrade view in the UI.

Requirements (Miguel, 2026-10-13): compatibility checks that warn and recommend but never hard-deny an old version;
pre-packaged upgrade files for the controller and the edges; configuration backups before upgrades to local disk, S3, FTP,
SSH and similar; upgrade checks in the UI; rollback is nice to have, and one-at-a-time upgrades may serve as the canary.

## Decision

1. **One release version, one compatibility table.** The controller, agent and SE image share a `MAJOR.MINOR.PATCH`
   release version (`switchyard.__version__`, the agent's `status.Version`, the template name). `domain/versions.py` (pure)
   holds `COMPAT`: the oldest agent the controller still pushes to (`min_agent`), the oldest agent that is *fully* supported
   (`recommended_min_agent`), the bundle format each agent version understands, and the schema revision each release
   expects. The table ships in the controller and in the upgrade bundle manifest.
2. **Warn, never block.** An engine older than `recommended_min_agent` gets a `warning` finding with the exact upgrade
   action; one older than `min_agent` gets an `error` finding but is still pushed to, using the oldest bundle format it
   understands, because an engine must keep serving. The only hard stops are ones that would damage state: a schema newer
   than the code (refuse to start the API, say why) and a migration without a fresh backup unless the operator passes an
   explicit override.
3. **Additive-schema rule (expand/contract).** A migration in release N must work with the code of N-1. Destructive
   changes (drop, rename, tighten) wait for N+1. CI upgrades a database to the previous release's head and runs the N-1
   suite against it. This is what makes one-node-at-a-time controller upgrades safe.
4. **Upgrade bundles.** `syctl release build` produces one signed archive per release: `manifest.json` (version, schema
   head, `COMPAT`, checksums, release notes), controller image tarball (or registry reference plus digest), proxy/UI image,
   Helm chart, Compose files, SE template (qcow2/vztmpl) with its agent and the agent binary on its own. The archive is
   signed with the same ed25519 scheme as config bundles and carries an SBOM (ADR 0200). `syctl upgrade apply BUNDLE`
   verifies it, runs the checks, backs up, migrates and rolls the nodes. Air-gapped sites need nothing but the file.
5. **Backups before upgrades.** A `backup` service exports the controller's *configuration* (tenants, objects,
   revisions, settings, `secret_ref`s, never secret values) as an encrypted archive and writes it through a typed
   `BackupTarget` adapter: `local`, `s3` (any S3-compatible endpoint), `sftp`/`ssh`, `ftp`/`ftps`. Credentials are
   `secret_ref`s into OpenBao. Targets are system settings; an upgrade runs the backup job and records its checksum and
   location in the audit log. Restore is `syctl backup restore`. This complements pgBackRest (point-in-time restore of the
   whole database), it does not replace it: the archive is portable across clusters and readable by `syctl`.
6. **Upgrade checks.** `GET /system/upgrade/check` (system admin) returns findings, each with a severity, code, subject
   and action: controller and database version, pending migrations, every engine's agent version against `COMPAT`, template
   age, last backup age, group health score, drift, certificate and OpenBao state, and database replication lag. The UI
   (Admin, Controller, Upgrades) shows the same list, offers "Back up now", and shows the roll in progress. Checks never mutate.
7. **Rollout as a canary.** Controller nodes and engines upgrade one at a time. After each step the roll waits for the
   health gate (group score of 80 or more, as ADR 0083) and **pauses on failure**, leaving the old and new members both
   in place. That pause is the rollback decision point: an operator resumes, or backs out the single new member.
   Engine back-out is retire-new-keep-old (already how `replace-se.md` works). Controller back-out is "restore the
   previous image on that node", which is safe because of the additive-schema rule. Full database rollback stays a restore
   from backup; `syctl db downgrade` is added but only for migrations marked reversible.
8. **Engines stay replace-not-patch.** The roll automates the `upgrade.sh` loop as a job. Enrollment still needs secrets
   delivered into the guest, so the first slice keeps that step explicit for LXC/VM and investigates cloud-init
   delivery for VMs.

9. **Release feed and update check.** A feed is a small signed `releases.json` (versions, channel, date, notes URL,
   download URLs and digests, upgrade-from versions, `COMPAT`). The controller reads one or more feeds, each a system
   setting: a GitLab or GitHub release or raw-file URL, any HTTPS URL, or a local file imported at an air-gapped site. It is
   **off by default** and the first-run wizard asks. The worker fetches on a schedule (daily) through the configured
   proxy and verifies the ed25519 signature against the project key plus any operator-added keys. The UI shows the latest
   version, a banner when behind and the release notes; "Download" stages the bundle and verifies its digest. A failed fetch
   or bad signature is a finding, never an error for the controller. Nothing about the installation is sent; the request
   is a plain GET. Channels: `stable` (default) and `candidate`.
10. **Plan, path, windows, smoke test, notes, history.** `syctl upgrade plan` lists every step and changes nothing. A
    release declares the versions it can be upgraded from; a larger gap yields the list of hops as a warning, not a block.
    Rolls can be scheduled, paused, resumed and aborted, with the `management` tenant's engines last. A smoke test (login,
    a read-only API call, the VIP answers, no new drift) runs after each step and pauses the roll on failure. The
    manifest's release notes (new, deprecated, breaking, operator action) show before confirming. Every plan, backup, step
    and result is an audit event with a history view, and start, pause, failure and finish go to the existing alert
    destinations. After an upgrade the UI compares render fingerprints before and after and lists engines whose output
    changed.

## Slices (one merge request, built in this order)

1. Version table, release feed and update check, `GET /system/upgrade/check`, agent version on the engine status, schema-newer-than-code guard,
   additive-schema CI test. Migration `0045` (backup targets, upgrade run records).
2. Backup targets and the backup job (local, S3, SFTP, FTP), restore command.
3. Release bundle build/verify and `syctl upgrade apply` for Compose and Helm.
4. Engine roll job with the health gate and pause.
5. UI: Upgrades page, version badges and the recommend-upgrade banner.

## Consequences

- A release process is now part of the product: version bump, `COMPAT` update, release notes.
- Old engines keep working across a controller upgrade for at least one release, so operators can stage the engine roll.
- The additive-schema rule costs a release of delay on every destructive schema change.
- The feed needs a project signing key and a published `releases.json` (a GitLab or GitHub release asset or raw file).
- Not decided here: automatic upgrade scheduling, and in-place package upgrade of engines (rejected: engines are disposable).

## Decision record: SSH library (2026-10-13)

SFTP backup targets use **asyncssh** (dual-licensed EPL-2.0 or GPL-2.0-or-later; Switchyard uses it under EPL-2.0 as an
unmodified library dependency, which is compatible with distributing Switchyard under Apache-2.0). Chosen over paramiko
(LGPL-2.1) for its asyncio API and over shelling out to `sftp` (no Python dependency, but harder to test and to pin host
keys). Host keys are pinned by SHA-256 fingerprint in the target settings; there is no trust on first use. Tested against an
in-process asyncssh SFTP server. Miguel confirmed the choice. ADR 0200 has no permitted-licence list, so this is recorded here and in
[sbom-and-licensing.md](../sbom-and-licensing.md); it appears in the controller's SBOM like any dependency.
