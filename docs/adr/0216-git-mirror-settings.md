# 0216. Git mirror settings in the controller, token as a secret reference

Status: Accepted
Date: 2026-10-16

## Context

The Git mirror (every applied revision written as YAML to a GitLab repository) was configured only through `SWITCHYARD_GITLAB_URL`,
`_PROJECT` and `_TOKEN` on the controller. There was no API or screen for it, and the access token was a plain value in the process
environment. A missing variable silently turned the mirror off, so an operator could not tell why nothing reached Git.

## Decision

- The settings live in `system_setting` (key `git_mirror`): `enabled`, `url` (https, no credentials), `project`, `branch` and
  `token_ref`. The token is never stored; `token_ref` is a reference into the secrets backend, read each time the mirror writes. A
  token value pasted into the reference is rejected by validation.
- `GET/PUT /system/git-mirror` and `POST /system/git-mirror/test` (system administrators, audited as `git_mirror.configured`). Test reads the
  mirror branch and writes nothing.
- The worker reads the settings at write time, so a change applies without a restart. The outcome of the last write (time, commit, error)
  is stored and shown.
- The environment variables remain a fallback while nothing is saved, so existing deployments keep mirroring. Saving settings replaces
  them. The API reports where the effective settings come from (`settings`, `environment`, `none`).
- A mirror that is off or unconfigured does nothing; the queued job succeeds without writing. A failed write still raises and is retried.

## Consequences

- No migration: `system_setting` already exists.
- The mirror job no longer waits in the queue on workers without environment variables; it completes as a no-op while the mirror is off.
- Moving the token out of the environment is optional but recommended; docs/operating.md says how.
