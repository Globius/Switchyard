# Release notes

One optional file per release, `X.Y.Z.json` (for example `0.2.0.json`), picked up by `tools/release/bundle.sh` and shown to the operator
before they confirm an upgrade (Admin > Upgrades, and `syctl release verify`). Keep each line to one sentence.

```json
{
  "new": ["Upgrade checks, backups and signed bundles"],
  "deprecated": [],
  "breaking": ["The old /foo endpoint is gone; use /bar"],
  "action_required": ["Run a backup before upgrading"]
}
```

`breaking` and `action_required` are the ones people act on, so say what they must do. A release without a file has empty notes.
Nightly builds have no notes file.
