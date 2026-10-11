# 0070. Client certificate verification (mutual TLS)

Status: Accepted
Date: 2026-10-11

## Context

ADR 0067 left mTLS out because the certificate objects only held server certificates with keys. The 01 mapping lists client
certificate verification (06 planned-vs-built).

## Decisions

1. A `certificate` has `purpose`: `server` (default, as before) or `client_ca`. A client CA holds CA certificates only (no key, no
   names, no renewal, no OCSP) and may name a revocation list in `crl_secret_ref`. A server certificate must still name a host.
2. `POST /tenants/{t}/certificates/import-ca` (`{name, pem, crl_pem?}`) checks the bundle (CA certificates only, none expired, no private
   key, a PEM CRL if given), stores it like other bundles in OpenBao (`.../<serial>`, CRL at `.../<serial>-crl`) and adds the object to a
   change set. It needs the same role as other certificate imports.
3. A TLS port may name `client_ca_id` and `client_verify` (`required`, the default, or `optional`). `required` makes HAProxy refuse
   clients without a valid certificate; `optional` lets them in and rules can look at `ssl_c_verify`. The validator reports
   `wrong_certificate_purpose` when `certificate_ids` hold a client CA or `client_ca_id` names a server certificate; `client_ca_id` needs
   `tls` and is forbidden on UDP.
4. The renderer adds `ca-file /etc/switchyard/certs/ca-<name>.pem verify <mode>` (and `crl-file .../ca-<name>.crl`) to the bind. The agent
   bundle carries those two files (certificates only; a bundle that holds a key is refused). The `haproxy -c` sandbox makes placeholder CA and
   CRL files, like it does for server certificates.
5. Not built: mTLS from HAProxy to the pool members (a client certificate on server lines) and passing client certificate fields to the
   backend. Both can build on the same objects (a server certificate for the first, `ssl_c_*` headers for the second).
6. Tables (migration 0018): `certificate.purpose`, `certificate.crl_secret_ref`, `vs_port.client_ca_id` (same-tenant foreign key, so a
   client CA in use cannot be deleted) and `vs_port.client_verify`.
