# 0107. Client certificate to pool members (backend mTLS)

Status: Accepted
Date: 2026-10-12

## Context

ADR 0070 left mTLS from HAProxy to the pool members out (decision 5, 06 planned-vs-built).

## Decisions

1. `Pool.client_certificate_id` names a `server`-purpose certificate (one with a key). HAProxy presents it on every TLS connection to the
   pool's members. It needs `tls: true` (model validator). A client CA is refused with `wrong_certificate_purpose`; a missing or foreign
   certificate with `unknown_reference` or `wrong_tenant`.
2. The renderer adds `crt /etc/switchyard/certs/<name>.pem` to the server options. The certificate is in the group's snapshot view and in
   `referenced_certs`, so the agent bundle writes the same `<name>.pem` file as for a front-end certificate; one certificate can serve both.
3. UDP pools reject it. Verifying the member's certificate stays `tls_verify`.
4. Table (migration 0028): `pool.client_certificate_id`, a same-tenant foreign key, so a certificate in use cannot be deleted.
5. The `haproxy -c` sandbox copies a throwaway bundle to every `crt` path on a server line, as it does for crt-lists.
