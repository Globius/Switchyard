# 0016. Job model: stages, log, retry and cancel

Status: Accepted

## Context

`GET /jobs/{id}` returned `{kind, state, attempts, error, result}`. The UI needs a Jobs page and live
apply progress (ticket T5 in `docs/design/05-frontend-backend-tickets.md`).

## Decisions

1. **Stages live on the job row.** `job` gains `started_at`, `change_set_id`, `stages` (JSON list of
   `{name, state, started_at, finished_at, error}`) and `log` (JSON list of `{at, level, message}`, the
   latest 500 lines). The apply pipeline announces `lock, render, validate, canary, rollout, commit` as
   `pending`, then reports each as `running`, `succeeded` or `failed`; stages never reached become
   `skipped`. With several SE groups a stage keeps one entry (first start, last finish). The worker
   writes with the admin role, because the application role may not update jobs. Each attempt resets
   the stages; the log keeps every attempt.
2. **Failed is not retrying (ADR 0007, changed by ADR 0223).** A reverted or rejected apply ends as `dead`
   with the reason in `error` and is never retried (it was `succeeded` with `ok: false` before ADR 0223). `retrying` is true only while a failed attempt waits for its next
   try (`queued` with `attempts > 0`). `ok` is copied from the result for kinds that report one.
3. **Retry and cancel.** Both need the operator role, are audited (`job.retried`, `job.cancelled`) and
   are tenant-scoped. Retry accepts `dead` (or `failed`) jobs and gives them a fresh attempt budget.
   Cancel accepts `queued` jobs only: a running job may be halfway through a rollout and is never
   interrupted. Cancelling an apply job moves its change set from `applying` to `failed`, so it can be
   applied again. `cancelled` is a new job state.
4. **Listing.** `GET /tenants/{t}/jobs?state&kind&cursor&limit`, newest first. The order is
   creation time, then id (UUIDv7 ids are only millisecond-ordered), and the cursor carries both. System jobs (no tenant) are not listed. The old
   `GET /jobs/{id}` keeps working.
5. **Progress delivery is polling.** Clients poll `GET /tenants/{t}/jobs/{id}`. A `job_progress` SSE
   event is a follow-up once the UI needs it; the stored stages are what it would carry.
