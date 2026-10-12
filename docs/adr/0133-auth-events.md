# 0133. Auth events: what is logged and where it goes

Status: Accepted
Date: 2026-10-12

## Context

Plan item E4: login, deny, logout and revoke must show up in the audit trail and the logs, tenant-scoped, the way controller
actions do.

## Decision

There are two sources, both already on the engine, so nothing here depends on the controller:

1. **HAProxy request logs** (`option httplog`) carry the outcome. A request denied by the login is a 401 or 403 from the second
   hop; a request sent to the proxy is logged with backend `auth_<service>-<set>`; the login endpoints (`/oauth2/start`,
   `/callback`, `/sign_out`) are logged with that backend too. Together they give *login started*, *login completed* (the
   redirect from the callback), *logout* and *deny* per service, which is per tenant because services belong to one tenant.
2. **oauth2-proxy's own log** (`auth_logging = true`): one line per sign-in result with the user and the client address, written to the
   journal and shipped by Vector to Loki like the HAProxy log (telemetry block P5). Request logging is off to avoid a second copy of
   every request.

Event names the log pipeline and the alert rules (block P6) use: `auth.login`, `auth.deny`, `auth.logout`, `auth.session_refresh_failed`.
They map to: proxy sign-in success line; HAProxy 401 or 403 from the second hop; sign-out request; proxy refresh error line.
The Vector transform that parses them and adds `tenant` and `service` labels belongs to the logs block; this record fixes the
names and sources so that block has a contract.

**Revoke.** Ending one user's sessions before they expire needs a deny list the engine can check; the signed cookie has none.
What exists today: rotate `cookie_secret_ref` (ends every session of the set) or lower `cookie_refresh_s`, after which a disabled
account fails the next refresh. A per-user revoke list is not built.

**Configuration audit** is unchanged: creating, changing or attaching an access set is an ordinary change set with an audit entry
(who, what, diff) and the secrets are references.

## Consequences

- No new controller endpoint or table. Until the log pipeline lands, the events exist only in the engine logs.
- The 401 and 403 of the second hop look like origin responses to a client; the log distinguishes them by backend.

## Alternatives considered

- **Engines post auth events to the controller.** Breaks the rule that the data plane does not depend on the controller, and
  creates a write path from engines into the audit store.
