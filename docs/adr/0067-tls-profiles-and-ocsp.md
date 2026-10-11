# 0067. TLS profiles and OCSP stapling

Status: Accepted
Date: 2026-10-11

## Context

The 01 mapping lists cipher and TLS-version profiles and OCSP stapling. The global `defaults` only set `ssl-min-ver TLSv1.2`, and a
port had no way to ask for more or less (06 planned-vs-built).

## Decisions

1. `VsPort.tls_profile` is `modern` (TLS 1.3), `intermediate` (TLS 1.2 and 1.3, forward-secret AEAD ciphers) or `legacy` (TLS 1.0 to 1.3,
   Mozilla's old list) and needs `tls: true`. The bind line gets `ssl-min-ver`, `ciphers` (not for `modern`) and `ciphersuites`.
   A port without a profile renders exactly what it did before. The cipher lists are constants in the renderer, from Mozilla's
   server-side TLS guidance, and are reviewed with a golden case when the guidance changes.
2. `Certificate.ocsp_stapling` (default false) writes `[ocsp-update on]` on the certificate's `crt-list` line, so the engine fetches and
   staples OCSP responses itself. The certificate needs an OCSP responder URL and its issuer in the bundle, so the model refuses it on
   `cloudflare_origin` certificates, which have none.
3. Tables: `vs_port.tls_profile` (checked against the three names) and `certificate.ocsp_stapling` (migration 0017).
4. **mTLS to clients and to backends is not built.** It needs a CA bundle to reach the engines, which the certificate objects and the
   agent bundle do not carry (they hold server certificates with keys). It will come with its own ADR once a CA object exists.
