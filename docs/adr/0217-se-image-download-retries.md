# 0217. Retry the GitHub downloads in the SE image build

Status: Accepted
Date: 2026-10-16

## Context

The `se-image` CI job failed on most runs on 2026-10-16 (8 of 11 on the runner pool, on identical code, with passes in between).
Every failure is the same step: the `waf` stage of `se-image/test/Dockerfile`, where `curl` fetches the OWASP Core Rule Set
tarball from GitHub and exits with code 35 (TLS handshake failure). The pass/fail mix on unchanged commits, the same failure
on scheduled pipelines before the base image changes in the pilot fixes (ADR 0214), and the passing `sbom-se-image` job that runs
the same Dockerfile show a flaky network path, not a regression. `docker build -q` hides curl's own output, so the log shows only the exit code.

## Decision

Every `curl` that downloads a release from GitHub in the SE image build (`se-image/test/Dockerfile`, `se-image/fetch-binaries.sh`)
uses `--retry 5 --retry-all-errors --retry-delay 3`. The pinned SHA-256 checks in `provision.sh` are unchanged, so a retry cannot
weaken integrity: a corrupt or substituted download still fails the build.

## Consequences

- A transient handshake failure costs a few seconds instead of a red pipeline.
- A real outage still fails, after about 15 seconds of retries.
- If exit 35 persists after this change, the cause is the runner's egress (MTU or proxy), not the repository.
