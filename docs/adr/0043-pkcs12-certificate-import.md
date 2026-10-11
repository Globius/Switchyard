# 0043. PKCS#12 certificate import

Status: Accepted
Date: 2026-10-11

## Context

Operators often hold a certificate and key as a `.p12`/`.pfx` file. Converting it to PEM by hand
pushes key material through extra tools and shells.

## Decisions

1. `POST /tenants/{t}/certificates/import` accepts `pkcs12_b64` and `password` as an alternative to
   `pem`; exactly one source is allowed, and `password` is only valid with `pkcs12_b64`. The `pem` path is unchanged.
2. The controller unpacks the file in memory with `cryptography`, writes `leaf + chain + PKCS#8 key` and runs the
   same `inspect_pem` checks (key matches leaf, validity, names). What is stored in OpenBao is the
   same unencrypted PEM bundle as before, so nothing downstream changes.
3. The password is a `SecretStr`, used only to decrypt, and is never logged, audited, stored or echoed.
4. Errors are 422 problem+json with a field path: `password` for a wrong password, `pkcs12_b64` for bad base64,
   an unreadable file, a file with no key or certificate, or legacy encryption.
5. **Legacy PFX (RC2/3DES) is refused** with a message to re-export using AES, unless the linked OpenSSL
   can read it. We do not enable OpenSSL's legacy provider: it would widen the attack surface for old ciphers.

## Consequences

The UI can offer a `.p12` upload. Files larger than about 192 KB are rejected by the field limit.
