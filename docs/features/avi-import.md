# Avi importer

**What.** Reads an NSX ALB (Avi) configuration export and turns it into one **draft** change set of Switchyard objects, with
a finding (`info`, `warning`, `manual`, `skipped`) for every decision.

**How to use.** `POST .../imports/avi` with the export, the target SE group and a VIP pool or an explicit VIP map. It is a
dry run by default; nothing is applied until you review and apply the change set. The full guide:
[Import from Avi](../import-avi.md).

**Limits.** VIPs do not carry over: every service gets a new address. WAF, DataScripts, bot and security policies, external
monitors and some persistence kinds are reported as `manual`. Never commit an Avi export or anything derived from it with
addresses, certificates or keys.

**Decision:** [0150](../adr/0150-avi-importer.md).
