# 0136. Client certificate identity

Status: Accepted
Date: 2026-10-12

## Context

Plan item E6. ADR 0070 lets a TLS port verify client certificates against a client CA. That proves *a* valid certificate; services
also want to know *whose* it is, require different people for different paths, and combine the certificate with a login.

## Decision

A policy set of `kind: client_cert` maps certificates to identities and checks them per path.

- `identities` (up to 500): `{by, value, user, groups}`. `by` is `cn` (the subject common name, for a CA that issues one
  certificate per person) or `sha1` (the certificate's SHA-1 fingerprint, 40 lower-case hex digits, for individual certificates such
  as a robot's). Names use `A-Z a-z 0-9 . _ @ : -`. `user` and `groups` are the identity the origin sees.
- `rules` (1 to 32, first match decides, no match means unchecked): `{match, mode}` where `mode` is `valid` (any certificate the port's
  client CA accepted), `identity` (the certificate must be in `identities`) or `groups` (and its identity must have one of `groups`).
- `user_header` (default `X-Cert-User`) and `groups_header` (default `X-Cert-Groups`, comma separated; `null` turns one off). Both are
  removed from every request first; they are written only for a request whose certificate maps to an identity.

**How it renders.** Identities become map files `haproxy/maps/<set>-certsha1.map` and `<set>-certcn.map` with lines
`<key> <user>|<groups>`. The frontend looks the fingerprint up first (`ssl_c_sha1,hex,lower`), then the common name
(`ssl_c_s_dn(cn)`), only when `ssl_c_used`; a hit sets the identity flags the rules and the headers read. Failing a rule answers 403.

**Requirements.** The service needs a TLS port with `client_ca_id` (`client_cert_needs_client_ca`); with `client_verify: optional`
the rules decide which paths need a certificate, with `required` every connection needs one. A certificate the CA does not accept
fails the handshake, before any rule. Revocation uses the CA's CRL (ADR 0070). One client-certificate set per service
(`multiple_client_cert_sets`); identity header names may not collide with those of an `access` or `jwt` set.

**Two factors.** A service may attach a `client_cert` set and an `access` set: the certificate rules run first (stage 4b), the login
after (stage 5b), so a request needs both. Their identity headers differ by default.

## Consequences

- Common-name mapping trusts the client CA to issue names only to the people they name; a CA that lets users choose their CN
  must be mapped by fingerprint.
- Subject alternative names are not used as identity yet (HAProxy exposes them as a list; matching one needs its own design).
- OCSP for client certificates is not built; the CRL covers revocation.
- The maps change with the policy set through a normal change set; HAProxy reloads on the new maps.

## Alternatives considered

- **Map in the backend with the whole certificate forwarded.** Pushes parsing into every origin and defeats the point.
- **Put identities in OpenBao.** Names and groups are not secret; the change set already gives review and audit.
