# Change sets, revisions and drift

**What.** Every change to a tenant's configuration is a **change set**: a list of create, update and delete operations that is
validated, diffed and applied as one. An apply locks the SE group, renders, validates with the real `haproxy -c`, pushes to
one **canary** engine, watches it for `canary_seconds`, then rolls out to the rest, and records a numbered **revision**
(mirrored to Git).

**How to use.**

- `POST .../change-sets`, add operations to `.../{cs}/ops`, read `.../{cs}/diff` (with impact and warnings), then
  `.../{cs}/apply`. Or send one object with `?apply=true`, or use `syctl api apply`.
- Tenants can require **approval** (four eyes): `request-approval`, then another person `approve`s or `reject`s.
- `POST .../revisions/{n}/rollback` applies an earlier revision through the same pipeline.
- **Drift** (an engine running something other than the desired state) is reported in `GET .../status/engines`, never fixed
  silently. `POST .../se-groups/{group}/reapply` pushes the desired state again, canary first.
- Concurrent edits are caught with `If-Match` and versions (412 on conflict).

**Limits.** An apply never reaches all engines at once. If the canary fails, the apply stops and the canary goes back to the
previous configuration. Engines keep serving their last configuration if the controller is down.

**Decisions:** [0003](../adr/0003-change-op-model.md), [0007](../adr/0007-change-pipeline-and-engine-contract.md),
[0046](../adr/0046-revision-message.md), [0069](../adr/0069-drift-reapply.md),
[0074](../adr/0074-agent-bundle-binding.md).
