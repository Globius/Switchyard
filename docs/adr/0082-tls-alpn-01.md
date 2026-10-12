# ADR 0082: TLS-ALPN-01 challenges

Status: Accepted

## Context

DNS-01 needs a DNS provider and HTTP-01 needs port 80 (ADR 0080). Some deployments have only port 443
open to the internet and cannot change DNS from the controller. RFC 8737 validates a name over TLS on
port 443 with the ALPN protocol `acme-tls/1` and a certificate that carries the SHA-256 of the key
authorization in a critical `acmeIdentifier` extension. The `acme` library no longer implements it.

## Decision

- Every HTTP service with a TLS port renders `alpn h2,http/1.1,acme-tls/1` (or without `h2`). No browser
  or API client offers `acme-tls/1`, so nothing changes for them.
- The solver builds the challenge certificate itself (self-signed, one DNS name, critical
  `acmeIdentifier`) and answers the challenge with an empty JSON object. It recognizes the challenge
  by its type name, because the library treats it as unrecognized.
- The controller loads that certificate into each enabled engine's HAProxy through the Data Plane
  API's runtime calls (no reload): a certificate is created and loaded, then added to the crt-list of
  every TLS service of the SE group for the name being validated, with the option `alpn acme-tls/1`.
  It is removed again as soon as the CA has validated, and always on failure.
- Wildcard names are refused (RFC 8737). The name has to lead to a VIP of an HTTP service with a TLS
  listener on port 443.

## Consequences

- **Visitors of that name are interrupted.** HAProxy picks the certificate by name, not by ALPN, so
  while the challenge runs every handshake for the name gets the challenge certificate. The request
  check warns about names that already serve HTTPS; the interruption lasts the few seconds validation
  takes. Use DNS-01 or HTTP-01 for names that must not blink.
- Services that are plain TCP with TLS (no HTTP mode) are not covered.
- Engines need the rendered `acme-tls/1`; one that runs an older configuration asks for an apply first.
- Entries added at run time are lost on a reload, which is what a failed cleanup needs.
- **One certificate per engine, an entry per crt-list.** A group with several TLS services has one
  crt-list each. The challenge certificate is loaded into the engine **once** and added to every
  crt-list; loading it per crt-list made HAProxy answer "Certificate ... already exists" and the
  order failed. A repeated call replaces what an earlier failed run left, and a failure unloads
  everything it loaded.
