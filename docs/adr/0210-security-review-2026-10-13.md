# ADR 0210: Fixes from the 2026-10-13 security review

Status: Accepted

## Context

The third security review (`docs/security/2026-10-13-review.md`) found issues where one tenant's input could
reach something shared, plus dependency advisories in the Go modules. These are the decisions behind the fixes.

## Decision

- **Reserved certificate names.** An engine is named by a tenant administrator, and the agent accepts a caller
  whose certificate has an allowed common name or DNS name (`sy-agent -controller-cn`). Enrollment now refuses to
  sign for an engine whose name is the controller's (`switchyard-controller` plus `SWITCHYARD_CONTROLLER_CN`, a
  comma-separated list). The `agents` PKI role in the bootstrap scripts gets `client_flag=false`: agent
  certificates are server certificates and never need to authenticate as a client.
- **A PKI role has one owner.** `acme-corp-devices` starts with `acme-` as well as `acme-corp-`. The longest tenant
  name wins (`pki_role_owner`); the snapshot validator and the device service refuse a role owned by another tenant.
- **Endpoints must be globally routable unless allow-listed.** `check_endpoint` (DNS providers, Kubernetes
  clusters, JWKS sync) blocked loopback, link-local and RFC 1918, but not shared address space (`100.64.0.0/10`,
  which holds some clouds' metadata address). Anything that is not global needs the operator's CIDR list.
- **Grafana paths are normalised before the deny list.** `?`, `#`, `;`, empty and dot segments and control
  characters are refused, and the deny list ignores case.
- **A placeholder metrics token stops startup** (`change-me`, or fewer than 16 characters).
- **Go modules** move to Go 1.26.9, `x/net` 0.60.0 and `grpc` 1.83.2. CI gets warning-only audit jobs for the Python,
  Go and UI lock files.

## Consequences

- An engine named `switchyard-controller` cannot enroll; rename it. If the controller's certificate has another name,
  set `SWITCHYARD_CONTROLLER_CN` to it on the controller.
- Existing OpenBao installations keep the old `agents` role until an operator runs
  `bao write pki/roles/agents ... client_flag=false`. The code fix does not depend on it.
- Sites that point DNS providers or Kubernetes clusters at `100.64.0.0/10` addresses must list the range in
  `SWITCHYARD_DNS_CIDRS` / `SWITCHYARD_K8S_CIDRS` / `SWITCHYARD_JWKS_CIDRS`.
