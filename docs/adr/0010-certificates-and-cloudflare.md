# 0010. Certificates, OpenBao and the Cloudflare allow list

Status: Accepted

## Context

M5 completes phase 1: certificates must be imported or issued, kept out of PostgreSQL, delivered to
the SEs, and renewed; the Cloudflare-only allow list must stay current.

## Decisions

1. **Key material only in the secrets backend.** `OpenBaoSecrets` reads and writes KV v2 with AppRole
   login (token renewed on 403). The `certificate` object keeps only `secret_ref`. Every stored
   certificate gets a new reference ending in the leaf serial number, so a renewal changes the object
   and the apply pipeline sees a real difference to roll out and, if needed, to revert.
2. **PEMs travel in the signed agent bundle.** The design allows keys to reach an SE "only inside the
   HAProxy cert push or a signed agent bundle". We use the bundle: `certs/<name>.pem` next to the
   routing files. The pipeline sends certificates *before* the HAProxy config when they changed, so
   HAProxy never loads a config naming a file that is not there yet. Engines without an agent are
   assumed to have certificates provisioned out of band (nothing is sent, nothing fails).
   The agent manages only `*.pem` files directly inside `certs/` (mode 0600) and reloads `haproxy`
   when they change. The Data Plane API keeps its crt-lists in the same directory, so those files are
   never touched or removed. `pipeline/bundles.py` builds the file set for both the pipeline and the
   status poller so drift detection includes certificates.
3. **Issuance.** Keys and CSRs are generated in the worker (ECDSA P-256). Sources: ACME DNS-01 through
   the Cloudflare adapter (one order per certificate, never per SE; the `acme` library, tested against
   Pebble) and Cloudflare Origin CA. Uploads are checked: the key must match the first certificate, the
   certificate must be valid now, every name must pass the hostname rules, no encrypted keys.
4. **Automation uses change sets.** Issuance, renewal (daily, 30 days before expiry, automatic
   certificates only) and the Cloudflare IP sync (daily, allow lists with `managed_by: cloudflare`)
   open ordinary change sets as `system:certs`, so they are validated, audited, canaried and
   revertible. When a tenant requires approval the change set waits for a person.
5. **API.** `POST /certificates/import` (adds the create to a change set), `POST /certificates/issue`
   and `POST /certificates/{id}/renew` (return job ids).

## Not covered

- OCSP stapling, authenticated origin pulls (mTLS from Cloudflare) and hot-swapping through the
  Runtime API: a changed certificate currently causes a graceful `haproxy` reload.
- Expiry alerting beyond the failed-renewal audit event.
- Certificates for engines without an agent.

## Update: certificates are staged before validation

HAProxy checks that every certificate file named in the configuration exists, so validating a first
TLS service on an engine failed before the certificate bundle had been sent. The pipeline now sends the
bundle to the canary first when a certificate is new there, then validates and pushes. The canary is
recorded as pushed before that, so a rejected configuration reverts it. The optional Docker sandbox
receives the same `certs/` files.
