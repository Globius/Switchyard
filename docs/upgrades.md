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

## Planned (ADR 0205)

Release feed and update check, signed upgrade bundles, configuration backups to local disk, S3, SFTP and FTP, a dry-run plan, the
engine roll as a job with a health gate and pause, and the Upgrades page in the UI.
