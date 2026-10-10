# 0011. Agent enrollment and certificate renewal

Status: Accepted

## Context

`sy-agent` serves gRPC over mTLS, so every agent needs a server certificate that the controller trusts,
and the controller's client certificate must be trusted by the agent. Certificates are short-lived.
The design says an agent trades a one-time token for a certificate from the OpenBao PKI through the
controller.

## Decisions

1. **The agent makes its own key.** `sy-agent enroll` generates an ECDSA key, sends a CSR and receives
   the signed certificate, the CA, the public key that verifies bundles and a renewal token. The private
   key never leaves the VM. Files are written atomically and only after the whole exchange worked.
2. **Names come from the engine record.** The controller ignores the names in the CSR and signs the
   engine's name and management address. A stolen token cannot obtain a certificate for another host.
3. **Tokens.** An administrator creates a one-time token for one engine
   (`POST /tenants/{t}/engines/{id}/enrollment-token`, shown once, valid for one hour; a new token voids
   the previous one). Tokens are random, stored only as SHA-256 and work once. All credential problems
   give the same `401` so the endpoint reveals nothing. `POST /enroll` needs no bearer token because the
   enrollment token is the credential.
4. **Renewal.** Each successful enrollment returns a renewal token (90 days, single use, rotated on
   every use). `sy-agent renew` uses it, so a systemd timer can renew without a person. Certificates last
   seven days by default. The agent reloads the certificate from disk on each handshake, so no restart
   is needed.
5. **Signing.** `PkiSigner` has an OpenBao PKI implementation (`<mount>/sign/<role>`) and an in-process
   development CA. Signing and consuming the token happen in one transaction, so a CA outage does not
   burn the token.
6. **Bundle signing key.** The worker needs the Ed25519 bundle key (`SWITCHYARD_BUNDLE_KEY_FILE`);
   until now the production wiring never loaded it, so agents were not used. Its public half is handed to
   agents at enrollment.

## Not covered

- Delivering the token into a freshly cloned VM. Proxmox cannot upload cloud-init snippets through its
  API, so this belongs to the SE template and its cloud-init design (for example a snippet written to
  the node, or the token passed by the operator tooling). `deploy_se_group` does not mint tokens yet.
- Rate limiting of `POST /enroll` (tokens are 192-bit random, so guessing is not practical, but the
  endpoint should still sit behind the reverse proxy's limits).
- Revoking an agent certificate before it expires; the short lifetime is the control for now.
