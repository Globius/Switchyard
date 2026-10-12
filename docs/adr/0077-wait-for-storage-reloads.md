# 0077. Wait for the reload a storage call starts

Status: Accepted
Date: 2026-10-11

## Context

`test_e2e_real_engines::test_good_change_reaches_both_engines_and_a_bad_one_is_reverted` failed in about one full run in four with "HAProxy restarted
during the canary window", and `test_reload_whose_new_worker_cannot_start_is_reported` flaked. Logs of the engine showed a third HAProxy worker
that nobody asked for, about a second after the push.

## Cause

Creating a crt-list through the Data Plane API answers `202` with a `Reload-Id` even with `force_reload=false`: the API reloads HAProxy after its reload
delay (one second). The apply pipeline stages the files twice (validate, then push), so on an engine that had never held the crt-list the delayed reload
landed after the configuration push, replaced the worker again, and the canary (which compares the worker pid over its window) read it as a restart. The
same stray reload could also make `push` believe its own reload had produced the new worker. Timing decided whether it coalesced with the push.

## Decisions

1. The adapter waits for the reload a storage call starts: the call's `Reload-Id` until it succeeds, and then for the worker to change (the API reports
   "succeeded" once the reload command exited, before the new worker runs). The wait uses the pid taken before the call.
2. The canary check is unchanged: a worker that changes during its window is still a failure. Its message now names the two workers.
3. Tests: `test_staging_and_pushing_files_leaves_no_stray_reload_behind` stages and pushes to a fresh engine and watches the worker for three seconds
   after each step (it fails without the fix, every time); the "new worker cannot start" test now takes its reference pid after staging, because
   staging legitimately replaces the worker once.
