# 0069. Re-apply the desired state to an SE group

Status: Accepted
Date: 2026-10-11

## Context

Drift (an engine running something other than the desired state) is reported and never corrected on its own. The UI needed the one
explicit action: put the group back to what the database says. A change set cannot do it: the desired state has not changed, so an empty
change set pushes nothing and a rollback to the live revision is refused ("already equals").

## Decisions

1. `POST /tenants/{t}/se-groups/{group}/reapply` (202, job `reapply_se_group`) pushes the tenant's current desired state to every engine of
   the group again. It uses the apply pipeline's stages (lock, render, validate, canary, rollout) and the canary-first rule, so it is never
   pushed to all engines at once. It makes no revision and changes no desired state.
2. It needs the role that may roll back (`tenant-admin`), because it overwrites whatever the engines run now.
3. What the engines held before is unknown, so there is nothing to revert to: the push has no "previous" configuration. A push the engine
   rejects leaves that engine on what it ran, and the job result says `ok: false` with the error. HAProxy validates before it switches.
4. The outcome is a `deployment` row without a change set (pointing at the latest revision) and audit events `se_group.reapply_requested`,
   `se_group.reapplied` and `se_group.reapply_failed`. The status poller clears the drift at its next cycle.
5. The job result is `{ok, se_group_id, se_group, revision_number, error, details}`; `revision_number` is the revision that was pushed (0 when
   the tenant has none yet).
