<!--
SPDX-FileCopyrightText: 2026 The Switchyard contributors

SPDX-License-Identifier: Apache-2.0
-->

# 0223. Bundle every certificate a crt-list names; a failed change set fails its job

Status: Accepted
Date: 2026-10-17

## Context

Two faults met in one incident. An engine was handed a rendered `certs/<service>.crtlist` that named a certificate
file the engine did not hold, because the certificates in the agent bundle were chosen separately from the crt-lists the
renderer writes. HAProxy refuses a crt-list with a missing file, so the canary rejected the configuration and the
change set was reverted. The apply job nevertheless ended as `succeeded` with `ok: false` in its result, so
anything that watched the job state (alerts, the job list, scripts) saw a green job for a failed change.

## Decision

1. **Bundle from the crt-lists.** `engine_files` receives the group's rendered files. Besides the certificates the
   services and pools reference, it bundles every certificate that a rendered `certs/*.crtlist` names, matched by
   file name. A crt-list that names a path outside `/etc/switchyard/certs` or a certificate the tenant does not have
   is a `BundleError`, so the change set is rejected at planning time, before any engine is touched. The apply
   pipeline and the status poller pass the same rendered files, so what is pushed and what is compared agree.
2. **A failed change set fails the job.** The `apply_change_set` and `reapply_se_group` handlers raise when the
   report is not `ok`. The job ends `dead` with the reason in `error`. `apply_change_set` jobs are enqueued with
   `max_attempts=1`: the pipeline has already reverted the engines, and the change set is not `applying` any more, so a
   retry could only fail again. This supersedes the "finished job, not a retry" rule of ADR 0007 and ADR 0016.

## Consequences

`GET /jobs` shows `dead` (no `ok`) for rejected or reverted applies; a job with state `succeeded` always has
`ok: true`. The OpenTofu provider already treats `dead` as an error. A tenant that relied on `ok: false` inside a
`succeeded` job must read the state instead.
