# Backend security review

Date: 2026-10-11. Scope: `controller/` (API, services, adapters, renderers, pipeline, jobs), the agent channel as the controller
uses it, the render templates, and dependencies. Method: reading the code against ADR 0030 and CLAUDE.md, targeted searches (SQL
construction, subprocess, deserialisation, outbound URLs, logging), and the existing test suites. The Go agent source and the UI were
not reviewed here, and no live system was attacked. Severity: **High** (cross-tenant or credential exposure), **Medium**, **Low**.

Summary: 1 High and 6 Medium findings, all fixed with tests; 4 Low fixed; 6 Low or informational findings stay open (deployment settings and
by-design behavior). No known vulnerable dependency (`pip-audit`, 2026-10-11). The Go agent and
the SAML and OIDC validation were reviewed in a second pass (see below).

## Fixed in this review (tests in `tests/unit/test_security_fixes.py` and `tests/integration/test_api.py`)

| # | Sev | Finding | Fix |
| --- | --- | --- | --- |
| F1 | High | **Cross-tenant secret references.** `certificate.secret_ref`, `crl_secret_ref`, BGP `md5_ref` and an engine's `credentials_ref` accepted any OpenBao path. A tenant could name another tenant's certificate key (`kv/tenants/<other>/...`) or a platform secret (`kv/proxmox/token`); the controller reads it and sends it in the engine bundle, or as Data Plane API credentials to an engine URL the tenant chose. | New rule `check_secret_ref_scope` (ADR 0071): `kv/tenants/<name>/...` must be the tenant's own, and `kv/proxmox/`, `kv/platform/`, `kv/system/`, `kv/controller/` are refused. Enforced in the snapshot validator (`secret_ref_not_allowed`, so it blocks apply), engine registration and SE group deploy. |
| F2 | Medium | **Plaintext agent channel by omission.** With `SWITCHYARD_BUNDLE_KEY_FILE` set and `SWITCHYARD_DATAPLANE_CA` unset the agent client used an insecure gRPC channel; bundles carry private keys. | `runtime.directory()` refuses to start in that combination unless `SWITCHYARD_INSECURE_AGENT=1`. |
| F3 | Low | **Open redirect after login** with a control character: `return_to=/<TAB>/evil.example` passed `safe_return_to`, and browsers drop the tab, giving `//evil.example`. | `safe_return_to` requires printable characters. |
| F4 | Low | **CSV formula injection** in the audit export: an actor, object or diff starting with `=`, `+`, `-` or `@` runs as a formula in a spreadsheet. | Such cells are prefixed with a quote. |

## Fixed in the second pass (ADR 0072; tests in `test_hosts.py`, `test_login.py`, `test_security_fixes.py`)

| # | Sev | Finding | Fix |
| --- | --- | --- | --- |
| O1 | Medium | **Engine URL was an SSRF primitive for tenant administrators**; `http://` accepted; raw connection errors shown to tenants. | Hand-registered engines need `https` and IP addresses (`dataplane_url`, `mgmt_ip`, `agent_url`) inside a `mgmt` network of the group or `SWITCHYARD_ENGINE_CIDRS`; names are refused. Tenants see a fixed sentence in `GET /status/engines`; system administrators and the log keep the real error. `SWITCHYARD_ENGINE_POLICY=open` for labs. |
| O2 | Medium | **No login throttling; anyone could lock break-glass; TOTP codes were replayable.** | Failures back off in the database (30 s doubling to 15 min) per source address and per account *from that address*; failed OIDC and SAML callbacks count against the source; the account is no longer locked as a whole. Each TOTP step works once. `SWITCHYARD_TRUST_FORWARDED=1` behind a proxy. |
| O3 | Medium | **Secret namespaces were a convention.** | Tenant objects may name `kv/tenants/<own>/` or `SWITCHYARD_SHARED_SECRET_PREFIXES` only (`open` policy for labs), enforced in the validator and at engine registration. `syctl openbao-policy` prints the matching OpenBao policy for the controller token. |
| A1 | Medium | **Agent enrolment accepted `http://`.** The answer carries the CA, the bundle signing key and the renewal token, so anyone on the path could take over the agent's trust. | `sy-agent enroll/renew` and `enroll.Run` require https; `--insecure-plain-http` exists for throwaway labs. |
| A2 | Medium | **Client CA revocation lists were refused by the agent** (a bug in my mTLS feature, found in this review): only `*.pem` files were manageable under `certs/`. | `certs/` now manages `*.pem` and `*.crl` (Go test added). |
| A3 | Low | **Bundles were not bound to an engine or a sequence**: a captured, correctly signed older bundle could be sent again (rollback), or sent to another engine. | The signed bundle names the engine and carries an increasing sequence; the agent refuses other engines and sequences at or below the last applied one (kept on disk). Unbound bundles need `-allow-unbound-bundles` (ADR 0074). |
| A4 | Low | **Any certificate from the agents' CA could call the agent API** (`GetStatus`, `ProbeMember`). | `sy-agent -controller-cn <name>` limits callers to the controller's client certificate name; unset keeps the old behavior and logs a warning. |

## Open findings

| # | Sev | Finding | Recommendation |
| --- | --- | --- | --- |
| O4 | Low | **Unauthenticated API documentation and OpenAPI** (`/api/v1/docs`, `/openapi.json`). | Acceptable internally; restrict at the proxy otherwise. |
| O5 | Low | **`/metrics` and service discovery expose management addresses, tenant ids and names** to the holder of `SWITCHYARD_METRICS_TOKEN` (constant-time compare; it opens nothing else). | Rotate it with Prometheus' credentials; one token per scraper if needed. |
| O6 | Low | **A system administrator can act in any tenant.** By design; audited under their name. | Alert on `system-admin` writes inside tenants. |
| O7 | Low | **Distributed guessing of break-glass** from many addresses is slowed per address only. The password and the TOTP code both have to be right. | Rate limit at the proxy or the network edge; keep break-glass off except during an incident (it is off by default and can be switched on for a time). |
| O8 | Low | **`redirect_uri` is built from the request's host** when `SWITCHYARD_PUBLIC_URL` is unset, so a forged `Host` header changes it (the provider still checks it against its registered list). | Always set `SWITCHYARD_PUBLIC_URL` in production (it also decides the `Secure` cookie flag). |
| O9 | Info | **Outbound URLs set by system administrators** (OIDC issuer, SAML metadata and endpoints, ACME, Cloudflare, Proxmox, Prometheus, GitLab) are trusted configuration. | Keep them out of any tenant-writable setting. |

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

- **Go agent.** Every bundle path is validated (clean, relative, inside a managed directory, allowed suffixes in `certs/`); files are written
  atomically with `0640` or `0600` for keys; only a fixed unit allow-list is reloaded; the runner starts only `systemctl`, `vtysh` and `ipvsadm`
  without a shell; limits on file count and size; the previous files come back if a reload fails. The server requires TLS 1.3 and a client
  certificate. Probes refuse targets that are not configured members.
- **OIDC.** Authorization code with PKCE (S256), state kept server-side and used once, nonce, issuer and audience checked, an allow-list of
  asymmetric algorithms. `tests/unit/test_oidc_hostile.py` feeds it wrong issuers, audiences, nonces, expiry, missing claims, a token from another
  key, `alg: none`, an HMAC token keyed with the public key (algorithm confusion) and garbage; all are refused.
- **SAML.** Signed responses or assertions only, verified against the configured certificates, and only the signed element is used (signature
  wrapping test), exactly one assertion, issuer, audience, validity window, `InResponseTo`, recipient and destination checked, replay cache, DTDs
  refused, SP-initiated only. The existing tests cover tampering, wrapping, replay, expiry, unsigned and IdP-initiated responses.

## Second review (2026-10-11): !85, !86, !89, !91 and the UI in !87, !88, !90

Scope: LXC guest kind and the Proxmox adapter changes (!85), the two-engine lab and data NIC addressing (!89), engine addresses on reads (!91), the bundle
binding (reviewed in the first pass, ADR 0074), the deploy and NIC address UI. Method as before; the UI diffs were searched for raw HTML, `eval`,
storage and link handling (none: React renders text, links go through the router). Fixed in ADR 0075 with tests:

| # | Sev | Finding | Fix |
| --- | --- | --- | --- |
| S1 | Medium | **A deploy could clone any guest.** `template_vmid` is tenant input and the adapter cloned whatever it named, so a tenant administrator could copy another tenant's running engine (disk, agent key, enrolment, Data Plane credentials) into a guest on their own network. | The adapter refuses a guest that is not a Proxmox template (`template: 1`). |
| S2 | Medium | **Node and pool names went into Proxmox request paths unchecked** (`/nodes/<node>/...`), so `../` or a query in `nodes` could redirect a call made with the controller's Proxmox token. | Strict patterns in the API model and again when the job reads its payload. |
| S3 | Medium | **Engine NIC addresses could equal service VIPs** (the deploy ignored VIPs, and `next-free` ignored engine addresses): a duplicate address takes the service down. | Both directions now count the other as used; `engine_addresses` is returned with the pool usage. |

Open (design level):

| # | Sev | Finding | Recommendation |
| --- | --- | --- | --- |
| S4 | Medium | **`guest_kind: lxc` is available to every tenant administrator in production.** The UI asks for an acknowledgement, but the API does not; the template decides whether the container is privileged, and a privileged container with nesting means root in the guest is root on the Proxmox host. | Only enable it in labs: a controller setting that refuses `lxc` unless set (and an unprivileged template where possible). Not done here so as not to break the running lab. |
| S5 | Low | **The lab enrolment script keeps one-time tokens** in `deploy/lab/e2e/work/` and `/root` of the Proxmox container after use (files are `0600` inside the guest). | Delete them after `pct push` (not changed here: that directory belongs to the operations work). |
| S6 | Low | **`HTTP 500` with "does not exist" or "no such" is treated as "missing"** by the Proxmox adapter. A different 500 that happens to contain those words (a missing storage or node) could make a destroy or lookup skip as if the guest were gone. | Match the guest-specific messages only, or look the guest up in the cluster resources list. |
| S7 | Low | **A VIP can be typed onto an engine's address by hand**: the domain model cannot see engine addresses. | Show the conflict as a warning in the change set preview. |

## Not covered

The UI (XSS, token storage), OpenBao and Patroni configuration, the reverse proxy and TLS termination in front of the API, the SE image, and
a dynamic test against a running stack.
