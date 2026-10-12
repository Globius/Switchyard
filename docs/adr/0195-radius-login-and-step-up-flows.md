# 0195. RADIUS logins, a login gate, and multi-step (nFactor style) flows

Status: Accepted
Date: 2026-10-12

## Context

The audit of planned against built work ([2026-10-12](../audit/2026-10-12-planned-vs-built.md)) lists two gaps in the login
story: plan item E5 asked for RADIUS as a directory type, and E8 for ordered, conditional login flows like Citrix nFactor.
[0137](0137-directory-logins-saml-ldap-local.md) left RADIUS out because Dex has no RADIUS connector, and noted that "a stateless
login service of our own" stays possible. Dex does have an `authproxy` connector: it trusts three headers from whoever fronts its
`/callback`. A small service of ours can be that front.

## Decision

**A `radius` connector for `directory` policy sets.** `servers` (one to four, each `name`, `host`, `port` 1812, `secret_ref`,
`timeout_s`, `retries`, `require_message_authenticator`), `nas_identifier`, `group_attribute` (`filter_id` or `class`), `email_domain` (RADIUS has no address, so the
user gets `<name>@<email_domain>`; a name that already has an `@` is used as is), `signing_secret_ref` (the key for the gate's cookies)
and `login_timeout_s`. A server address is a host name or a routable IP; loopback, link-local, unspecified, multicast and reserved
addresses are refused, so a tenant cannot aim the gate at services on the broker host. Shared secrets and the signing key are
OpenBao references inside the tenant namespace like every other secret.

**The login gate (`syctl broker gate`).** For each RADIUS directory the broker still runs one Dex, now with an `authproxy`
connector. The controller's public proxy sends that directory's path to the gate (one process on loopback port 5599 for all RADIUS
directories), not to Dex. The gate

- serves the sign-in page and checks the person with a RADIUS Access-Request (PAP password, Message-Authenticator, retransmission,
  answers verified against the shared secret; a wrong secret, a forged answer or one without a Message-Authenticator is a failure,
  never a success. The Message-Authenticator is required by default because the Response Authenticator alone is open to the
  BlastRADIUS attack (CVE-2024-3596); `require_message_authenticator: false` is for a legacy server that cannot send one);
- forwards `/callback` and `/callback/<connector>` to Dex with `X-Remote-User`, `X-Remote-User-Email` and `X-Remote-Group` **only
  after** a valid sign-in, and removes those three headers from every request that comes from outside;
- passes every other path to Dex unchanged (discovery, keys, token endpoint).

It keeps no state: the sign-in in progress (`sy_flow`) and the finished sign-in (`sy_gate`, one hour, so one login serves several
services) are HMAC-signed cookies (`Secure`, `HttpOnly`, `SameSite=Lax`, path-scoped), bound to their purpose so one cannot be
replayed as the other. Any number of gate processes can serve one directory. The form carries a CSRF value (double-submit). Failures
are counted per user and address in memory: five in five minutes lock that pair (HTTP 429), thirty lock the address; the message is
the same for a wrong password and an unknown user. The return target must be a path under the directory's own prefix, so the gate is
not an open redirect. An unreachable RADIUS server answers 503 and signs nobody in.

**Challenges.** An Access-Challenge (for example "enter your one-time code") is shown to the person and answered on the same step
with the `State` attribute, up to three rounds.

**Flows (nFactor style).** A directory with a RADIUS connector may carry `flow.steps` (one to four): each step has `name`, `kind`
(`password` asks for user name and password on the first step and for a secret after that; `code` asks for a one-time code), `server`
(which RADIUS server checks it; default the first), `prompt`, and an optional `when` that must all hold for the step to be asked:
`groups_any` (groups returned by the steps already passed), `source_in` and `source_not_in` (networks of the client address). A
step whose condition fails is skipped. Without `flow` the directory has one password step on its first server. A condition that
cannot be evaluated never skips a factor (an unreadable client address does not satisfy `source_not_in`).

**Factor groups and step-up.** After n passed factors the person holds the groups `sy-factors-1` to `sy-factors-n`. The gate drops
any group a RADIUS server returns that starts with `sy-factors-`, so only the gate can issue them. An access rule may set
`min_factors` (2 to 4). On the loopback hop HAProxy then requires `sy-factors-<n>` among the groups the proxy vouched for; a session
without it is redirected (303, GET and HEAD) to `<broker>/<tenant>/<directory>/_gate/elevate?level=n&host=<host>&path=<path>`, other
methods get 403. The gate asks for the missing factors (conditions are ignored when a level is requested), then sends the person to
`https://<host><auth path>/start?rd=<path>`, which starts a fresh OIDC round that now carries the higher count. The host must be one
the directory's access sets serve and the path a plain local path. `min_factors` is accepted only on an access set that uses a
directory with a flow of at least that many steps (`step_up_needs_flow`, `step_up_too_deep`). The HAProxy renderer needs the broker
URL for this (`render_haproxy(..., broker_url)`, filled from `SWITCHYARD_AUTH_BROKER_URL` by the services that call it); without it
the render fails with a clear message, as for any directory login.

**Defaults** (chosen without asking; change by a new ADR): PAP only; no EAP, no accounting; no failover between servers (put a VIP
or a RADIUS proxy in front); the gate session lasts one hour; the sign-in in progress lasts five minutes (`login_timeout_s`); groups
come from `Filter-Id` unless `class` is chosen.

## Consequences

- The gate is, like Dex, a control-plane dependency of new logins and refreshes, not of established sessions or of traffic.
- RADIUS secrets and the signing key are in `gate/<tenant>-<directory>.json`, owner-only on the broker host, like the Dex configs.
- Only the engine's step-up redirect needs the broker address; services without `min_factors` render exactly as before.
- Verified against real Dex in `tests/integration/test_gate_dex.py` (code flow, group header parsing, two-step flow, forged
  `X-Remote-*` refused) and against a RADIUS server in-process (`tests/unit/test_radius.py`). Not verified against a vendor RADIUS
  server (FreeRADIUS, NPS, an OTP product) in the lab.

## Not built

EAP methods, CHAP, RADIUS accounting, RadSec (RADIUS over TLS), server failover, and a TOTP factor kept in Switchyard (the second factor is a RADIUS server). The UI has no editor for
directories yet (read-only list, [audit](../audit/2026-10-12-planned-vs-built.md)); use the API or `syctl`.

## Alternatives considered

- **A RADIUS-to-LDAP shim in front of Dex's LDAP connector.** Extra moving part, no groups, no challenges.
- **Compile a RADIUS connector into Dex.** A fork to maintain.
- **Our own OIDC provider.** More code than the gate and a second token implementation to secure.
- **Step-up by `prompt=login` on oauth2-proxy.** The proxy has no per-request parameter pass-through; the gate redirect carries the intent.
