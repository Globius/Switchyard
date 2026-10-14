# Public repository security review — 2026-10-13

## Executive summary

**Repository:** Globius/Switchyard, `main`, baseline `49eaec651f09643ab8ca351e0fb6752a2a809a16`.
**Authorization:** audit and fix confirmed issues; subsequently authorized to create a GitHub PR. The initial report was presented before publication. The fixes are committed on `codex/public-repository-security-sweep` and rebased onto `main` at `09dfa72e4707d7f91642e81d2e9392af480305f6`.

The review identified two high-severity issues: a stale contribution approval could forward unreviewed code to GitLab runners, and quickstart private keys were readable by other local users. Both have source fixes and regression coverage. Additional fixes protect outbound connections, signed archive extraction, backup writes, agent peer identity, and proxy/request metadata. Severity reflects plausible deployment conditions; it does not establish that exploitation occurred.

No confirmed live credential was found in the reviewed reachable Git history. This is a scoped assessment, not a guarantee that the public repository or a deployed installation is free of vulnerabilities. One medium-severity image advisory remains open pending a package-host network change and final image verification. GitHub/GitLab administrative security settings and production infrastructure could not be independently verified.

## Scope and method

Reviewed controller authentication/authorization, tenant boundaries, outbound adapters, release and backup handling, Go agent/provider/posture components, UI, deployment manifests, proxy configuration, both CI platforms, dependency advisories, current source and reachable Git history. Compared existing security reviews with current implementation rather than treating earlier deferred findings as still present. Current production PostgreSQL/Patroni/etcd configuration already includes TLS/authentication controls.

Used code review, targeted abuse regressions, existing unit/integration tests, real PostgreSQL and local TLS checks, container runtime smoke tests, Gitleaks, dependency audits and Trivy. History contained 757 reachable commits; Gitleaks reported 465 commits with scanned diffs. Reviewed 23 current and 27 historical image blobs with OCR; no strong credential pattern was found. OCR cannot establish absence of hidden or otherwise unreadable image content.

Historical scanner candidates were inspected as generated API example literals and invalid placeholder PEMs. Retained narrow fingerprint exceptions rather than excluding whole generated API files. A synthetic canary in a formerly excluded OpenAPI path was detected, demonstrating that the generated-file scan gap is closed. No real credential values are reproduced here.

## Findings and remediation

| ID | Severity | Finding and prerequisite | Change and reason | Status |
| --- | --- | --- | --- | --- |
| PUB-01 | High | A contributor could update a PR after its approval label; the bridge also fetched a moving ref. GitLab runners could receive code the maintainer had not reviewed. | Consume approval on the label event, invalidate it on revision/reopen, compare fetched commit to the event SHA and push that immutable SHA. | Source fixed; bridge/workflow regressions pass. |
| PUB-02 | High | Quickstart made administrative private keys world-readable and exposed the bootstrap token to terminal output; another local user could access them. | Restrictive umask, owner-only administrative files, explicit service group access only for required keys, symlink refusal, temporary issuance cleanup, and token file path output. Verify bootstrap TLS using its CA. | Source fixed; permission regressions pass. Existing exposed keys require operator rotation. |
| PUB-03 | Medium | An allowed hostname could rebind between validation and the actual Kubernetes/DNS/JWKS connection. | Resolve and validate all answers at connection time; pin the destination IP while retaining HTTP Host and TLS identity. RFC2136 uses a validated literal address. | Source fixed; rebinding and real TLS identity regressions pass. |
| PUB-04 | Medium | An external JWKS server or webhook receiver could occupy controller resources with oversized or slow replies. | Limit JWKS to 256 KiB and a total deadline, refuse compressed bodies/URL credentials/plain HTTP, stream webhook status without reading its body and enforce a total deadline. Close webhook connections to keep original hostname identity separate. | Source fixed; size, compression and slow-response regressions pass. |
| PUB-05 | Medium | A local actor able to modify the upgrade archive between signature verification and extraction could substitute image bytes. | Verify the actual extracted type, length and digest against the signed manifest; create destinations exclusively and remove invalid output. | Source fixed; archive replacement regression passes. |
| PUB-06 | Medium | The engine agent's default accepted any certificate from the configured CA without requiring the controller identity. | Default the expected certificate CN to `switchyard-controller`; explicitly disabling the check emits a warning. | Source fixed; positive/negative identity tests pass. |
| PUB-07 | Low | FTP backup paths omitted archive-name validation; newline matching and predictable local temporary paths weakened file safety. | Validate the complete name on all operations; use exclusive random owner-only temporary files and atomic replacement. | Source fixed; backup regressions pass. |
| PUB-08 | Low | Unbounded or malformed incoming request IDs could be echoed into responses/log context. | Accept only bounded ASCII alphanumeric/hyphen IDs; generate a replacement otherwise. | Source fixed; API regressions pass. |
| PUB-09 | Medium | Runtime images contained inherited package advisories; the old proxy ran as root with broad proxy-header trust. | Use smaller pinned Alpine runtime bases, remove inherited controller pip/ensurepip, run proxy as UID 101 with restricted runtime privileges, and require explicit trusted proxy addresses. Prepare the signed zlib package update. | Runtime controls validated; zlib update and exact final image builds remain pending. |

Relevant code: `tools/bridge/bridge.py`, `.github/workflows/bridge.yml`, `deploy/quickstart/{bootstrap.sh,secure-secrets.py}`, `controller/switchyard/adapters/{endpoint.py,notify.py,backup_targets.py}`, DNS/Kubernetes adapters, `services/{jwks_sync.py,release_bundle.py}`, `api/app.py`, `agent/cmd/sy-agent/main.go`, controller/proxy Dockerfiles and deployment templates.

## CI changes on GitHub and GitLab

| Control | GitHub Actions | GitLab CI |
| --- | --- | --- |
| Dependency audit failures block the pipeline | Remove `continue-on-error` from Python, Go and UI audit jobs. | Remove `allow_failure` from the same three audit jobs. |
| Secret scanning covers generated API files | Uses shared tightened Gitleaks configuration/fingerprints. | Uses the same shared configuration/fingerprints. |
| Runtime image changes | Release script builds shared controller/proxy Dockerfiles. | Same release script/Dockerfiles; prefetch now reads their base references instead of obsolete hard-coded names. |
| Documentation publishing privilege | Build has read permissions; only main deployment receives Pages/OIDC write permissions. | Existing Pages job already runs only on the default branch; retained that gate. GitLab has no GitHub token permissions map. |
| Reviewed GitHub contributions reaching GitLab | Bridge approves an immutable reviewed commit and consumes the label. | Receives the checked immutable commit. Runner isolation/protected variables remain an administrative verification item. |
| External GitHub actions | Pin every external action to a verified commit SHA; add weekly update configuration. | No GitHub actions are used. GitLab job images are not all digest-pinned; full CI toolchain reproducibility remains follow-up work. |

The shared deployment and regression changes apply irrespective of CI host. Local workflow tests check audit enforcement on both hosts. Neither hosted pipeline was triggered during this audit. The fact that an audit job fails does not prove repository branch-protection settings require it before merge.

## Validation completed

- Controller fast suite: 2,495 passed, four skipped before the last two webhook regressions; the final focused outbound suite passed all 26 tests. Selected domain/render/pipeline coverage was 91%, above the 85% gate.
- Authentication/API/enrollment checks: 70 passed. Real PostgreSQL/RLS/apply/render smoke checks: 32 passed.
- UI: 460 tests across 49 files passed; dependency audit found no reported advisories.
- Agent, posture agent and provider Go tests passed; agent/provider vet passed. Corrected only the missing generated SPDX header that caused the provider baseline test failure.
- Python lint/format and strict typing passed. Python locked runtime dependencies and all three Go modules had no reported dependency findings. Go scanning used the official vulnerability repository snapshot dated October 10 because this environment blocked the normal database endpoint.
- Controller candidate ran as UID 10001 with read-only root/no added capabilities, loaded native dependencies, served authenticated PostgreSQL-backed API requests and revoked its temporary test token. Proxy ran as UID 101 with read-only root/no capabilities; UI/CSP/API routing, explicit proxy trust and spoof-resistant rate limiting passed.
- Helm rendering/security checks passed. History scan passed with reviewed placeholder fingerprints. No scanner result is treated as proof of exploitability or absence of all vulnerabilities.

## Open findings and release conditions

**PUB-09 residual — Medium:** both Alpine candidate runtime images still contain `zlib` 1.3.2-r0, reported as **CVE-2026-85091**, fixed in 1.3.2-r1. Trivy reported no high/critical findings in these candidates and no controller Python dependency advisories. The tracked Dockerfiles contain `apk upgrade --no-cache zlib`, but the environment proxy rejects `dl-cdn.alpinelinux.org`. Test builds explicitly omitted that update and used injected build-only proxy CA trust; therefore they do **not** establish that the exact final Dockerfiles build or that zlib is patched.

An environment draft allowing `dl-cdn.alpinelinux.org` was saved. It requires review/save and **publication of the environment** before the policy applies. After publication, build both unmodified Dockerfiles, repeat runtime smoke checks and vulnerability scans, and confirm the fixed zlib version. Do not describe the candidate images as vulnerability-free or production-ready on the current evidence.

**Unverified hosted dependency alert:** the GitHub push response reported one moderate Dependabot alert on the default branch ([alert 1](https://github.com/Globius/Switchyard/security/dependabot/1)). Its package, affected range and relation to this branch could not be retrieved through the blocked API; it is not marked resolved by the local scans. Review it before merge.

**External verification:** GitHub API access was denied by this environment's network policy. Branch protections, required checks, secret scanning/push protection, private vulnerability reporting, release environments, GitLab protected variables/runner boundaries and public visibility of historical artifacts require administrator review. No production cluster, Proxmox endpoint, Kubernetes installation, external tenant endpoint or disaster-recovery drill was exercised.

**Deployment considerations:** the controller base changes from Debian/glibc to Alpine/musl. Frozen native wheels/imports and a real PostgreSQL API smoke passed, but production integrations and HA behavior still need deployment validation. Agent certificate CN defaults now reject a non-controller identity; deployments using a different controller certificate name must set the expected name explicitly. Existing quickstart installations should rotate private keys/tokens that may have been readable, and rebuild before restart. Forwarded client addresses require explicit trusted proxy CIDRs; a broad private-network trust list is no longer the default.

## Change scope and publication

Changes span shared security boundaries, both CI configurations, deployment templates, operational documentation and regression tests. There are no application feature additions or dependency lockfile rewrites. The provider generated-header correction is a baseline repair and is not a security finding. ADRs 0211 and 0212 record the decisions. This report was provided before PR/MR publication; the open image verification and administrative checks remain visible rather than marked resolved. The rebase retained the newer GitLab release publishing and temporary test-directory changes.
