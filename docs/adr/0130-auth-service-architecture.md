# 0130. Authentication service: oauth2-proxy in front of the frontend, loopback re-entry, closed by default

Status: Accepted
Date: 2026-10-12

## Context

Plan items E1 and E2 ([08](../design/08-competitive-parity-plan.md)): put a login in front of a virtual service, with OIDC,
session cookies and per-route rules, and keep the data-plane promises of CLAUDE.md: the SE keeps serving when the controller
is down, the agent has no arbitrary-command surface, no secrets in PostgreSQL, no free-form config. The work-block doc
([09](../design/09-parity-work-blocks.md)) asks this record to settle the sidecar: oauth2-proxy first, a small Go service in
`agent/` only if SAML, per-route rules or the fail mode need it.

HAProxy has no built-in "ask a service first" step (no `auth_request`). The usual add-ons are a Lua script or an SPOE agent;
Lua is out of scope (plan D6) and an SPOE agent would be a new service to write and secure.

## Decision

**oauth2-proxy is the default sidecar, used as a reverse proxy that HAProxy sends requests to and that hands them back.**
No Lua and no SPOE. For each virtual service that attaches an `access` policy set (an *auth instance*):

1. The frontend gets a second bind on `127.0.0.1:<loop_port>` (18000 + the service's position among the group's access
   services, sorted by name) and a backend `auth_<service>-<set>` with one server, `127.0.0.1:<auth_port>` (14000 + the same
   position). The engine runs one oauth2-proxy per config file in `/etc/switchyard/auth/`, listening on `<auth_port>` with
   `upstreams = http://127.0.0.1:<loop_port>`.
2. A request that needs a login (decided by the set's rules, [0131](0131-access-rules-and-authorization.md)) is routed with
   `use_backend auth_...` before normal routing. The login endpoints (`<auth_path>/start`, `/callback`, `/sign_out`, default
   `/oauth2/`) always go to the proxy.
3. oauth2-proxy authenticates (OIDC code flow with PKCE, signed and encrypted session cookie), then forwards the request to
   the loopback bind with the user in `X-Forwarded-User`, `-Email` and `-Groups`. The same frontend sees it again: the
   *second hop*. A request is on the second hop when `dst` is `127.0.0.1` and `dst_port` is the loop port; HAProxy records that
   in a per-request flag (`txn.sy_loop`) before any other rule.
4. On the second hop the engine runs only what must run after authentication: it recovers the real client address from
   `X-Forwarded-For` (`set-src`), checks the identity against the rule ([0131](0131-access-rules-and-authorization.md)),
   writes the identity headers ([0132](0132-identity-headers.md)) and routes normally to the pool. Every earlier stage of
   [0100](0100-policy-model-and-request-stages.md) (rate limits, redirects, fixed responses, request rewrites, response
   rewrites) runs once, on the first hop, and its rules carry `!{ var(txn.sy_loop) -m bool }`. `X-Forwarded-Proto` is set on
   the first hop only, so the origin sees `https`.
5. The allow list's connection filter (`enforce_allow_list`) exempts `dst 127.0.0.1`, because the second hop comes from the proxy.

**Fail mode: closed.** If the proxy is down, `auth_...` has a server that refuses connections and HAProxy answers 503; the origin
is never reached without a login. Public rules still work (they never touch the proxy). The loopback bind refuses a request
that lacks the decision header (403), so a local process cannot skip the login by connecting to it. Nothing here depends on the
controller: sessions are signed cookies, the IdP is reached from the engine, and the config is a file on the engine.

**Configuration and secrets.** The access set holds the OIDC settings (`issuer`, `client_id`, `client_secret_ref`,
`cookie_secret_ref`, scopes, groups claim, cookie name, lifetime and refresh interval). Secrets are OpenBao references checked
against the tenant's namespace (ADR 0071); the controller resolves them when it builds the engine bundle and writes
`auth/<service>-<set>.cfg` (owner-only) in the signed agent bundle. They never go into PostgreSQL, `haproxy.cfg` or the rendered
views (which show `REDACTED`). A missing or malformed secret stops the rollout (`BundleError`), because an unauthenticated
proxy must never start.

**Agent.** `auth` is a new managed directory mapped to the unit `sy-auth` (flat `*.cfg` files, mode 0600). A change restarts the
unit (oauth2-proxy reads its config at start; sessions survive in the cookies); removing the last file stops it. The SE image
(plan block P7) installs oauth2-proxy and ships `sy-auth.service`, which starts one process per file in `/etc/switchyard/auth/`.
That unit and the package are the only image work this record asks for.

**Tenant identity provider object.** The OIDC settings live inside the access set, not in a separate object. One set can be
attached to many services of a tenant, which covers the common case (one IdP per tenant). A separate `identity_provider`
object can be extracted later without changing the rendered output.

## Consequences

- Two HAProxy passes per protected request (plus the proxy hop); fine for admin UIs and internal apps, expensive for bulk traffic,
  which is why public rules exist and why protection is per rule.
- `option httplog` logs both hops; request logs show the second hop with the user's headers removed after use.
- Port ranges 14000 to 15999 and 18000 to 19999 on `127.0.0.1` are reserved on engines with access sets (1000 services each).
- A proxy per service means one cookie per host name; single sign-on across hosts of one domain is a cookie-domain setting
  that can be added to the set later.
- The identity headers can be trusted by an origin only if the service is reachable only through the engine (ADR 0132).
- HTTP/2 to the proxy and WebSockets work (oauth2-proxy proxies upgrades).

## Alternatives considered

- **Auth subrequest through Lua or SPOE.** Gives the nicest request flow (one pass) at the price of Lua or a new agent. Both
  stay available for later without changing the model: the access set describes *what* is required, not how it is enforced.
- **oauth2-proxy `--upstream` to the pool directly.** The upstream is one static address per process; routes, maps and several
  pools would have to be duplicated in the proxy. Re-entering the frontend reuses all of the routing.
- **A small Go service in `agent/`.** More control (SAML, per-route rules in-process, a fail mode) and a new security surface to
  maintain. Not needed for OIDC; revisit with E5.
- **Keycloak gatekeeper, Authelia, Pomerium.** Heavier or tied to their own policy languages.
