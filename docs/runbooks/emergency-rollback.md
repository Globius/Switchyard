# Emergency rollback

Use when a revision that is already live causes an outage. Prefer the normal flow; this is the fast path.

1. Find the last good revision: `GET /tenants/{t}/revisions` (or the UI History tab). The revision number is
   what the drift view calls "revision #N".
2. Roll back: `POST /tenants/{t}/revisions/{n}/rollback`. It opens a draft that restores that revision's
   objects. Review the diff.
3. Apply it. If the tenant requires approval and nobody can approve, a system administrator applies with a
   reason: `POST …/apply` with `break_glass_reason` (audited as `change_set.break_glass_apply`). The
   `management` tenant always needs this, and its `controller` service and SE group cannot be removed by a
   change (ADR 0051).
4. Every apply goes through the canary step, so one engine takes the change first; do not skip it. Watch
   `status/engines` and the 5xx rate while it proceeds.
5. If the controller itself is down, SEs keep serving the last config. Do not edit HAProxy by hand; fix the
   controller first (`restore-database.md` if the database is the cause).

**Confirm.** Traffic is healthy through the VIP, no drift is reported, and an incident note records the
revision numbers involved.
