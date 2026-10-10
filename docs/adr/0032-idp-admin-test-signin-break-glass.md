# 0032. Identity-provider administration, test sign-in, break-glass enrolment and system tokens

Status: Accepted
Date: 2026-10-10

## Context

ADR 0030 specified these; this records how they are built.

## Decisions

1. **Providers are one resource with two schemas.** `/identity-providers` takes a body discriminated
   by `protocol` (`oidc` or `saml`), as policy sets do by `kind`. `protocol` is required on create and
   on PATCH and cannot change. Fields of the other protocol are refused with their path.
2. **Secrets are write-only.** An OIDC `client_secret` goes to the secrets backend at
   `kv/idp/<name>` and the row keeps the reference; reads show `client_secret_ref` and
   `client_secret_set`. Without a writable backend the request is refused with the path
   `client_secret` and `client_secret_ref` is the alternative. Audit events list changed field
   names, never values.
3. **Discovery is probed, not required.** Creating or changing an OIDC issuer fetches its discovery
   document; failure is a `warnings[]` entry on that response.
4. **SAML metadata import** (`idp_metadata_url`, https only, no redirects, 1 MB; or
   `idp_metadata_xml`) fills entity id, the HTTP-Redirect SSO URL and signing certificates; explicit
   fields win. The XML is parsed with entity resolution and network access off.
5. **Signed requests.** `sign_requests: true` makes the controller generate an RSA-2048 key and a
   self-signed certificate (ten years) unless a key reference is given; the key lives in the secrets
   backend, the certificate in the provider and in the SP metadata (`AuthnRequestsSigned="true"`).
   Requests use the HTTP-Redirect signature (RSA-SHA256 over the query string).
6. **Test sign-in** stores a `login_state` with `purpose=test` and an `idp_test` row. The callback
   (OIDC or SAML) records claims, groups, `would_grant` and `warnings`, creates no session and no
   `user_account`, and redirects to `return_to` without a cookie. A provider refusal is a result
   (`error`), not an HTTP error. The result is deleted when read and after five minutes. A disabled
   provider can be tested.
7. **Break-glass.** Enrolment generates a 24-character password and a 160-bit TOTP secret, stores the
   secret in the secrets backend and returns both once with an `otpauth://` URI. The administrator
   cannot sign in until `confirm` receives a valid code; administrators made earlier by
   `syctl local-admin` count as confirmed. Enabling needs a confirmed administrator and a reason,
   lasts 1 to 1440 minutes (default 240), and expires by comparing a stored time with the clock.
   `SWITCHYARD_BREAK_GLASS=1` is reported as `forced_by_deployment` and cannot be undone through the
   API. Enrolling or deleting an administrator switches the setting off.
8. **System tokens** are `tenant_id IS NULL` rows listed under `/system/tokens`: 30 days by default,
   90 at most, shown once. Their use is audited as `token.system_used`, at most every five minutes
   per token. A token still cannot create tokens, so they are made by a signed-in person.
9. Deleting a provider deletes its mappings, users and their sessions (database cascade) and is
   audited.

## Consequences

- Migration 0013 adds `identity_provider.version`, `login_state.purpose/test_id`, `idp_test`,
  `local_admin.totp_confirmed_at/last_login_at` and `break_glass_setting`.
- The login callback can now return a test outcome instead of a session; both protocols share it.

## Alternatives considered

- **Run the test sign-in in the browser only**: no stored result, but then claims and `would_grant`
  would have to be computed client-side from raw tokens. Rejected.
- **Let the operator upload the SAML signing key**: more flexible, but the controller-generated key
  never leaves the secrets backend. A reference is still accepted.
