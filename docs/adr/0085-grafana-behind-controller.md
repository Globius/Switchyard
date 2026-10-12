# 0085. Grafana behind the controller, scoped per tenant

Status: Accepted
Date: 2026-10-12

## Context
The UI only linked to Grafana (`SWITCHYARD_GRAFANA_URL`); its own charts query Prometheus and Loki through the
controller. The design (03, "Grafana embedding") promised Grafana behind the controller's auth, with tenant isolation
on the data path, and the frontend ticket G4 listed the proxy as open. Design v52 draws the Metrics panels natively in
the UI (no iframes) and keeps "Open in Grafana" as a link, so Grafana is for deep dives in its own tab.

The requirement is stronger than "sign in once": a person must see only what their role in ADR 0030 allows, and even
someone who obtains a token and takes it straight to Grafana, or who is inside Grafana, must not see another tenant's
metrics or logs. Grafana cannot be the boundary: its data sources run with their own credentials, so a Viewer who types
`{tenant="other"}` into Explore gets the other tenant's series unless something below Grafana says no.

## Decision
The boundary is the controller, in three layers; Grafana's own authentication and authorization are not relied on.

1. **Data source proxy (the tenant boundary).** Each tenant has its own Grafana organization (`sy-<tenant id>`). Its
   Prometheus and Loki data sources point at the controller (`/internal/ds/prometheus`, `/internal/ds/loki`) and send a
   bearer token derived by HMAC from `SWITCHYARD_GRAFANA_DS_SECRET` that names the tenant and nothing else. For every
   request the controller (`domain/query_scope.py`, `api/grafana_proxy.py`):
   - accepts only the query, series, label and label-value endpoints (no `status/config`, `targets`, `rules`, admin,
     `federate`, write or `tail` paths);
   - rewrites every PromQL vector selector and every LogQL stream selector so `tenant="<id>"` is its **first** matcher,
     adds a selector to bare metric names, and adds the matcher to label and label-value calls that have none. Matchers
     are ANDed, so a caller's own `tenant` matcher can only narrow. The rewritten text is scanned again and must pass a
     check that every selector starts with the matcher; anything the scanner does not understand is refused (fail
     closed);
   - merges URL and form parameters into one list before rewriting and sends them in one place, so a second copy of
     `query` cannot be smuggled past the rewrite.

   The rewriter is tested against the real Prometheus, Loki and Grafana images
   (`tests/integration/test_grafana_stack.py`, `SWITCHYARD_STACK_TESTS=1`), including from inside Grafana as the
   tenant's own Grafana user without the proxy.
2. **Launch tickets and a proxy session (who gets in).** `POST /tenants/{t}/grafana/launch` (any role in the tenant;
   `POST /grafana/launch` for system administrators, who get the unscoped organization) returns a link that works once for
   60 seconds. Opening it makes the controller spend the ticket with one conditional `UPDATE` (two requests or two nodes
   cannot both win), prepare the organization, and set a random `sy_grafana` cookie (stored only as SHA-256, `HttpOnly`,
   `SameSite=Strict`, path `/grafana`) valid for 30 minutes, for that tenant only, and only while the browser session
   that asked is still valid. The session ends 30 minutes after its last use (the end moves out while the tab is in use,
   and the cookie is re-sent) and 8 hours after it started at the latest. Tickets and sessions live in `grafana_grant`.
3. **Reverse proxy (what Grafana sees).** `/grafana/**` is authenticated by that cookie alone. The proxy drops every
   header a browser could use to impersonate someone (`Cookie`, `Authorization`, `X-WEBAUTH-*`, `X-Grafana-*`,
   `X-Forwarded-*`), adds `X-WEBAUTH-USER` and `X-Grafana-Org-Id` itself, strips Grafana's `Set-Cookie`, and refuses
   Grafana's administration paths (`api/admin`, `api/orgs`, `api/users`, `api/serviceaccounts`, `api/teams`, snapshots,
   live). Grafana runs with `auth.proxy` (trusted only from the controller's addresses, `GRAFANA_PROXY_WHITELIST`), no
   login form, no anonymous access, no sign-up, no organization creation and no automatic org assignment; it is not
   published anywhere but to the controller.

Grafana users are created by the controller, one per person and tenant (`sy-<hash>`, no personal data), as **Viewer in
exactly one organization**. A real-stack test found that Grafana otherwise gives a new user an organization of their own
with the Admin role, in which they could add a data source that skips the controller; `confine_user` removes every other
membership and deletes such a personal organization at each launch.

The standard dashboard (`Virtual services`) is installed in each tenant organization from
`controller/switchyard/assets/grafana/`; Viewers cannot edit it. "Open in Grafana" in the UI asks for a launch link
and opens it in a new tab (landing on the dashboard filtered to the service). Nothing frames Grafana: the UI's CSP is
unchanged and the nginx front sends `frame-ancestors 'none'` on `/grafana/`, which it routes to the controller;
`/internal/ds` is not routed through it.

The native panels need one more API, added here: `GET /tenants/{t}/engines/{engine}/metrics` (CPU, memory, HAProxy
sessions and traffic of one engine, tenant and engine labels enforced), and an optional `until` on both metrics
endpoints for a custom range (`window_s` now goes up to 30 days).

All of it is off unless `SWITCHYARD_GRAFANA_INTERNAL_URL`, `_ADMIN_PASSWORD`, `_DS_SECRET`, `_DS_URL` and
`SWITCHYARD_PROMETHEUS_URL` are set; the UI then keeps the plain link.

## Consequences
- A leaked ticket is good for one use within a minute. A leaked proxy cookie reaches Grafana only through the
  controller and only for one tenant, for at most 30 minutes, and ends with the person's browser session. A leaked data
  source token (it lives in Grafana's database, encrypted) reads one tenant's data from the controller's internal port;
  keep that port off the user network. Changing `SWITCHYARD_GRAFANA_DS_SECRET` re-keys every tenant at its next launch.
- Role changes take effect when the grant ends (at most 30 minutes) because the proxy session is not recomputed per
  request; a revoked browser session ends the Grafana session at once.
- The controller needs Grafana's server-administrator password (organizations and users cannot be created with a
  service account). Treat `GRAFANA_ADMIN_PASSWORD` like a database password and keep Grafana reachable only from the
  controller nodes.
- Grafana is served from the UI's own origin (a separate tab, not a frame), so a script injected into Grafana runs with the
  UI's origin. Viewers cannot edit dashboards, plugins cannot be installed, and the dashboards come from the controller;
  a separate origin is the next step if that is not enough.
- The query rewriter is a scanner, not a PromQL parser. It is small and fails closed, and covered by table tests, a
  generative test and the real servers, but a new PromQL or LogQL construct that confuses it will surface as a refused
  query, not a leak.
- An unscoped organization for system administrators exists (`/grafana/launch`); its data sources are the
  file-provisioned ones that read Prometheus and Loki directly. Administrators are Viewers there.
- Not covered here: Grafana Live (WebSockets, switched off) and alerting inside Grafana.

## Alternatives considered
- **Grafana `auth.proxy` plus `prom-label-proxy` and a Loki label enforcer** (the original sketch in 03): two more
  services to run and secure, and a user who is Editor in their organization can still change the data source URL. Done
  here in the controller, which already owns the tenant model and the telemetry contract.
- **One shared organization and `X-Grafana-User` on data source calls**: avoids organizations, but whether Grafana
  forwards that header from every backend plugin depends on its version, and the proxy would have to derive the tenant
  from a header Grafana adds rather than from a token it cannot change.
- **Trusting Grafana teams, folders and data source permissions**: correct configuration is the only protection, and one
  mistake exposes every tenant.
- **A signed token instead of a ticket row**: simpler, but single use needs shared state anyway.
- **Loki's own multi-tenancy (`X-Scope-OrgID`)**: a real boundary for logs, but Prometheus has none, and one mechanism
  for both is easier to test.
