# 0007. Change pipeline and the engine contract

Status: Accepted
Date: 2026-10-10

## Context

M2 pushes configuration to real HAProxy engines through the Data Plane API. Testing against the
real API (`tests/integration/test_engine_contract.py`) showed behaviour the design did not predict.

## Decision

- **Pipeline** (`pipeline/apply.py`): plan (apply ops in memory, validate, render old and new, skip
  groups whose files do not change) → advisory lock per SE group → validate (sandbox `haproxy -c`,
  then the canary engine's `only_validate`) → canary push and watch → one-by-one rollout → one DB
  transaction (version-checked writes, revision, deployments, audit, Git-mirror job). Any failure
  after the first push reverts every engine that took the new files and marks the change set failed.
- **Reload success is verified.** The Data Plane API reports a reload as succeeded once the reload
  command exits, even if the new worker cannot start (for example it cannot bind a VIP) and HAProxy
  keeps the old worker and config. `DataPlaneEngine.push` therefore requires the worker pid to
  change. SEs need `net.ipv4.ip_nonlocal_bind=1`.
- **Fingerprint, not bytes.** The API rewrites `haproxy.cfg` but keeps comments, so the render
  embeds `# switchyard-render: sha256:<hash of all files>`. Drift detection compares it.
- **Files** live under `/etc/switchyard/` (`haproxy/…`, `certs/…`); names have no dots because the
  API rewrites dots in uploaded file names; crt-lists sit in the certificate directory.
- **Canary** = fingerprint matches, worker keeps running, no backend that had a serving server has
  none now, plus a routing probe (`NoRoutingProbe` reports "not checked" until the agent exists).
  Servers without health checks report `no check` and count as serving.
- **Commit after rollout.** Desired state is written only after the engines accepted it. If the
  commit loses an optimistic-lock race, engines are reverted. A brand-new SE group has no previous
  rendered config, so there is nothing to restore; the report says so.
- **Job semantics.** A reverted or rejected apply is a *finished* job (`ok: false`), not a retry.
  Only infrastructure errors raise and retry with backoff.
- **API** (`api/app.py`): bearer API tokens (hashed, expiring, role-scoped), RFC 9457 errors, ETag
  and `If-Match`, `?change_set=` draft reads with `draft_status`, `?apply=true` implicit change sets.
  `docs/api/openapi.json` is the committed contract; CI runs a breaking-change check against the
  target branch.

## Consequences

OIDC/SAML login (a later step) issues sessions in front of the same principal model. Certificates
and keys are not pushed yet; engines must already hold the PEM files named by the crt-lists
(M5 adds the OpenBao-backed provider).
