# 0072. Hardening: engine addresses, login throttling, secret namespaces

Status: Accepted
Date: 2026-10-11

## Context

The backend security review (docs/security/backend-review.md) found three design-level issues: a tenant administrator could point an engine
at any address (O1), logins had no per-source throttle and break-glass could be locked out by strangers (O2), and secret paths were only
guarded by convention (O3).

## Decisions

1. **Engine addresses (O1).** Registering an engine needs `https`, and `dataplane_url`, `mgmt_ip` and `agent_url` must be IP addresses (not
   names, which could be re-pointed) inside a `mgmt` network of the engine's SE group or inside `SWITCHYARD_ENGINE_CIDRS` (comma-separated, operator
   set). A group without a management network and no allow-list accepts no hand-registered engine. `SWITCHYARD_ENGINE_POLICY=open` turns the policy
   off for labs. Engines created by the deploy job get their addresses from the management pool and are not affected. Tenants see the fixed text
   "The controller could not reach or talk to this engine" in `GET /status/engines`; system administrators and the log keep the real error.
2. **Login throttling (O2).** Failed sign-ins are counted in the database (`login_throttle`, migration 0019) per source address and, for break-glass,
   per account *from that address*. After 5 failures (10 per source) a key is blocked for 30 s doubling up to 15 minutes; a success clears it; a gap
   of an hour resets the count. The account is no longer locked as a whole, so a stranger cannot lock the real administrator out from the
   administrator's own address. Failed OIDC and SAML callbacks count against the source and are refused with `/login?error=too_many_attempts` while
   blocked. The source is the socket peer, or the last `X-Forwarded-For` entry when `SWITCHYARD_TRUST_FORWARDED=1` is set behind a proxy.
   A TOTP code is accepted once: the last accepted time step is stored (`local_admin.totp_last_step`) and the same or an earlier step is refused.
3. **Secret namespaces (O3).** A tenant object may name `kv/tenants/<its own name>/...` or a prefix in `SWITCHYARD_SHARED_SECRET_PREFIXES`
   (comma-separated, each ending in `/`); anything else is refused (`secret_ref_not_allowed`, or 422 for engines), as is the platform's own
   namespace. `SWITCHYARD_SECRET_POLICY=open` allows other paths (labs). `syctl openbao-policy --shared kv/se/ --platform kv/proxmox/token` prints the
   matching OpenBao policy for the controller's token (read/write only under `tenants/*`, read-only on the shared and platform paths, deny on
   delete and destroy), so a bug in the code cannot widen what the token can read.
4. Operators of existing deployments must set `SWITCHYARD_SHARED_SECRET_PREFIXES` for any shared path in use (for example `kv/se/`) and
   `SWITCHYARD_ENGINE_CIDRS` for hand-registered engines outside a management network before upgrading; existing objects that break the rules
   fail validation at their next change.
