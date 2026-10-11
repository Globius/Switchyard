# Backend security review

Date: 2026-10-11. Scope: `controller/` (API, services, adapters, renderers, pipeline, jobs), the agent channel as the controller
uses it, the render templates, and dependencies. Method: reading the code against ADR 0030 and CLAUDE.md, targeted searches (SQL
construction, subprocess, deserialisation, outbound URLs, logging), and the existing test suites. The Go agent source and the UI were
not reviewed here, and no live system was attacked. Severity: **High** (cross-tenant or credential exposure), **Medium**, **Low**.

Summary: 1 High found and fixed, 3 Medium (1 fixed), 4 Low (2 fixed), and open design-level issues listed below. No known vulnerable
dependency (`pip-audit`, 2026-10-11).

## Fixed in this review (tests in `tests/unit/test_security_fixes.py` and `tests/integration/test_api.py`)

| # | Sev | Finding | Fix |
| --- | --- | --- | --- |
| F1 | High | **Cross-tenant secret references.** `certificate.secret_ref`, `crl_secret_ref`, BGP `md5_ref` and an engine's `credentials_ref` accepted any OpenBao path. A tenant could name another tenant's certificate key (`kv/tenants/<other>/...`) or a platform secret (`kv/proxmox/token`); the controller reads it and sends it in the engine bundle, or as Data Plane API credentials to an engine URL the tenant chose. | New rule `check_secret_ref_scope` (ADR 0071): `kv/tenants/<name>/...` must be the tenant's own, and `kv/proxmox/`, `kv/platform/`, `kv/system/`, `kv/controller/` are refused. Enforced in the snapshot validator (`secret_ref_not_allowed`, so it blocks apply), engine registration and SE group deploy. |
| F2 | Medium | **Plaintext agent channel by omission.** With `SWITCHYARD_BUNDLE_KEY_FILE` set and `SWITCHYARD_DATAPLANE_CA` unset the agent client used an insecure gRPC channel; bundles carry private keys. | `runtime.directory()` refuses to start in that combination unless `SWITCHYARD_INSECURE_AGENT=1`. |
| F3 | Low | **Open redirect after login** with a control character: `return_to=/<TAB>/evil.example` passed `safe_return_to`, and browsers drop the tab, giving `//evil.example`. | `safe_return_to` requires printable characters. |
| F4 | Low | **CSV formula injection** in the audit export: an actor, object or diff starting with `=`, `+`, `-` or `@` runs as a formula in a spreadsheet. | Such cells are prefixed with a quote. |

## Open findings

| # | Sev | Finding | Recommendation |
| --- | --- | --- | --- |
| O1 | Medium | **Engine URL is an SSRF primitive for tenant administrators.** `dataplane_url` is checked for scheme and shape only. The controller connects to it from its own network (with its client certificate) and shows the error text in `GET /status/engines`, so it can probe internal hosts and ports. `http://` is accepted, which sends the Data Plane credentials in clear. | Require the host to be inside a `mgmt` network of the SE group (when one exists) or in an operator-set allow-list of CIDRs; refuse `http://` unless a lab setting is on; return a generic error to tenants and keep the detail in the log. Needs a decision on engines registered by hand outside a mgmt network. |
| O2 | Medium | **Break-glass lockout can be triggered by anyone, and there is no per-source throttle.** Five wrong passwords lock the only emergency account for 15 minutes; `/auth/break-glass` and the OIDC/SAML callbacks have no rate limit. An attacker who can reach the API can keep break-glass locked during an incident. Accepted TOTP codes are not remembered, so a captured code can be replayed inside its window. | Per-source throttle in front of the lockout (reverse proxy or middleware), lockout keyed by source plus account, remember the last accepted TOTP step. The proxy rate limit belongs to the production deployment. |
| O3 | Medium | **Secret namespaces are a convention.** F1 blocks other tenants' paths and the known platform prefixes, but a tenant can still reference any other path, for example a shared `kv/se/dpapi` that is used on purpose. | Make the policy explicit: an OpenBao policy for the controller that reads only `kv/tenants/*` and named shared paths, plus a configured allow-list in `check_secret_ref_scope`. Design-level because existing deployments use shared paths. |
| O4 | Low | **Unauthenticated API documentation and OpenAPI** (`/api/v1/docs`, `/openapi.json`) describe every route. | Acceptable for an internal controller; restrict at the proxy if the API is exposed more widely. |
| O5 | Low | **`/metrics` and service discovery expose management addresses, tenant ids and names** to the holder of `SWITCHYARD_METRICS_TOKEN` (compared in constant time; it can do nothing else). | Treat the token as a secret and rotate it with Prometheus' credentials; consider one token per scraper. |
| O6 | Low | **A system administrator can act in any tenant.** By design, and every action is audited under the administrator's name. | Alert on `system-admin` writes inside tenants. |
| O7 | Info | **Outbound URLs set by system administrators** (OIDC issuer, SAML metadata and endpoints, ACME directory, Cloudflare, Proxmox, Prometheus, GitLab) are trusted configuration, not tenant input, so they are not an SSRF path for tenants. | Keep them out of any tenant-writable setting. |

## Checked and found sound

- **SQL.** All queries use SQLAlchemy expressions or bound parameters; there is no string-built SQL (search for f-string `text()` and
  `execute(f`). Raw `text()` appears only for fixed defaults in models and migrations. No `subprocess`, `eval` or unsafe deserialisation.
- **Tenant isolation.** Tenant routes resolve `{t}` through `TenantService.resolve`, which answers 404 for tenants the principal cannot see;
  services take the tenant id from that result and open `tenant_session` (RLS) for tenant data. `admin_session` is used only after a role
  check (system routes, jobs, audit export) or with an explicit tenant filter. `tests/integration/test_rls.py` and the permission matrix test
  (every route, every role, ADR 0030) cover this. The principal-name lookup resolves real names only for people in the tenant's own history.
- **Config injection.** Every user value reaching HAProxy, FRR or Keepalived goes through the validators and `render.escape` (`ident`, `arg`,
  `hostname`, `bind_address`, `wildcard_regex`), which refuse rather than escape. BGP passwords must match a safe pattern. The newer fields
  (`tls_profile`, `slow_start_s`, `passive_check`, `max_prefixes`, `client_verify`) are enums or bounded integers; the cipher lists are
  constants. Golden cases are validated by the real binaries.
- **Authentication.** API tokens are stored as hashes and looked up by hash (no timing oracle); unknown, revoked and expired tokens are
  indistinguishable. Session cookies are `HttpOnly`, `SameSite=Strict`, `Secure` behind https, with a CSRF header on cookie writes. The
  metrics token uses `hmac.compare_digest`.
- **Secrets in logs, database and Git.** Certificates and keys live in OpenBao; the database holds references. PKCS#12 passwords are not
  logged or stored. Client CA bundles carry no key and the bundle builder refuses a key in them. No logging statement includes secret material.
- **Agent channel.** Bundles are signed (Ed25519) and sent over mTLS gRPC; the monitor test refuses targets that are not configured members.
  See F2 for the one gap.
- **Dependencies.** `pip-audit` on the exported runtime requirements: no known vulnerabilities. Re-run in CI once it is back on.

## Not covered

The Go agent (file allow-list, unit allow-list, bundle verification), the UI (XSS, token storage), OpenBao and Patroni configuration, the
reverse proxy and TLS termination in front of the API, and a dynamic test against a running stack. SAML and OIDC validation were read only at
the level of their ADRs and tests (signed assertions, replay cache, issuer and audience checks); a focused review of `services/saml.py` and
`services/oidc.py` with hostile inputs is worth doing next.
