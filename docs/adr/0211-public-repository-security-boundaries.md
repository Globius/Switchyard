# 0211. Tighten public repository and runtime security boundaries

Status: Accepted
Date: 2026-10-13

## Context

The repository is now public. Review found overly permissive local key files, hostname validation that was separate from outbound connection establishment, uncontrolled external replies, and deployment/CI privilege that exceeded the required behavior. The public-repository review records the findings and validation limits.

## Decision

Keep administrative keys and tokens owner-only; grant service group read access only to required runtime keys. Validate all resolved addresses at outbound request time and pin connections while retaining original Host and TLS identity. Bound JWKS bytes and total request time, reject compressed responses, and avoid downloading webhook response bodies. Require the controller certificate identity by default and bound request identifiers.

Run the proxy as UID 101 with a read-only root filesystem and reduced privileges, trust only explicitly configured proxy addresses, and disable unused Kubernetes service-account token mounts. Use pinned smaller runtime images and remove inherited Python package installers. Keep the remaining zlib finding open until signed updates can be built and rescanned. Alpine/musl compatibility requires native dependency and deployment validation.

Make dependency scans blocking on both GitHub and GitLab. Use shared scanner configuration and Dockerfiles on both hosts; pin GitHub actions to immutable commits and restrict publishing permissions to the publishing job. Platform-specific controls and unverified administrative settings are stated explicitly in the report.

## Consequences

Operators must rotate previously exposed local credentials, set custom controller identities explicitly, and configure trusted proxy addresses. JWKS servers must support uncompressed JSON within the documented size/deadline limits. CI advisory or scan errors now require resolution before the affected check passes. A saved environment network draft has no effect until published; candidate builds that omit blocked package updates are not final release validation.

See [the public repository review](../security/2026-10-13-public-repository-review.md) and [ADR 0212](0212-reviewed-commits-and-verified-archive-bytes.md) for the separate bridge/archive/backup decisions.
