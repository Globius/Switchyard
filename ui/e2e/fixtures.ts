/** In-browser API mock: just enough of the tenant "acme" for the screens under test. */
import { test as base, type Page, type Route } from "@playwright/test";

const ME = {
  auth: "session",
  break_glass: false,
  csrf_token: "tok",
  email: "dana@example.com",
  id: "u1",
  is_system: false,
  name: "Dana Reyes",
  principal: "user:authentik:dana",
  session: null,
  tenants: [{ id: "t1", name: "acme", role: "tenant-admin", via: [] }],
};

const PERMS = {
  roles: ["viewer", "operator", "tenant-admin", "system-admin"],
  permissions: [
    { verb: "write", object: "application", min_role: "operator", description: "Edit." },
    { verb: "rollback", object: "revision", min_role: "tenant-admin", description: "Roll back." },
    { verb: "apply", object: "change_set", min_role: "operator", description: "Apply." },
  ],
};

export const VS = {
  id: "v1",
  tenant_id: "t1",
  name: "web",
  enabled: true,
  protocol: "http",
  se_group_id: "g1",
  vip: "10.0.0.20",
  vip_pool_id: null,
  ports: [
    { port: 80, tls: false, http2: false, proxy_protocol: false, https_redirect_port: 443 },
    { port: 443, tls: true, http2: true, proxy_protocol: false, https_redirect_port: null },
  ],
  certificate_ids: [],
  default_pool_id: "p1",
  routes: [
    {
      order: 1,
      match: { host: "app.example.com", path_prefix: "/" },
      pool_id: "p1",
      redirect: null,
    },
  ],
  policy_set_ids: [],
  allow_list_id: null,
  enforce_allow_list: false,
  restore_client_ip_header: null,
  version: 3,
  draft_status: "unchanged",
  changed_fields: [],
};

const page = (items: unknown[]) => ({ items, next_cursor: null });
const base_ = { version: 1, draft_status: "unchanged", changed_fields: [], tenant_id: "t1" };

const ROUTES: [RegExp, unknown][] = [
  [
    /\/auth\/providers$/,
    { break_glass: true, providers: [{ id: "authentik", name: "Authentik", protocol: "oidc" }] },
  ],
  [/\/me$/, ME],
  [/\/permissions$/, PERMS],
  [
    /\/tenants$/,
    [{ id: "t1", name: "acme", version: 1, require_approval: false, quotas: {}, usage: {} }],
  ],
  [
    /\/status\/virtual-services$/,
    [{ id: "v1", health: "healthy", members_up: 2, members_total: 2 }],
  ],
  [/\/virtual-services\/v1$/, VS],
  [/\/virtual-services$/, page([VS])],
  [
    /\/se-groups$/,
    page([
      {
        id: "g1",
        name: "edge",
        ha_mode: "bgp",
        maxconn: 50000,
        canary_seconds: 30,
        routing_profile_id: null,
        networks: [],
        ...base_,
      },
    ]),
  ],
  [
    /\/pools$/,
    page([
      {
        id: "p1",
        name: "app",
        algorithm: "roundrobin",
        members: [],
        health_monitor_id: null,
        ...base_,
      },
    ]),
  ],
  [/\/policy-sets$/, page([])],
  [/\/vip-pools$/, page([])],
  [/\/certificates$/, page([])],
  [/\/change-sets$/, page([])],
  [
    /\/revisions$/,
    page([
      {
        number: 3,
        summary: "Initial",
        author: "dana",
        created_at: "2026-10-10T10:00:00Z",
        git_sha: null,
      },
    ]),
  ],
  [/\/audit-events$/, page([])],
  [/\/audit-actions$/, []],
  [
    /\/schema\/virtual-service$/,
    {
      protocol: "http",
      algorithms: [],
      member_port_required: true,
      monitor_types: [],
      pool_forbidden: [],
      port_forbidden: [],
      service_forbidden: [],
    },
  ],
  [/\/jobs$/, page([])],
  [
    /\/summary$/,
    {
      services: { total: 1, healthy: 1, degraded: 0, down: 0, disabled: 0, unknown: 0 },
      failing_members: [],
      drifted_engines: [],
      unreachable_engines: [],
      expiring_certificates: [],
      jobs: { running: 0, queued: 0, failed: 0 },
      open_drafts: 0,
      pending_approval: 0,
      latest_revision: null,
    },
  ],
  [/\/engines$/, []],
  [/\/principals$/, []],
];

async function mock(route: Route) {
  const url = new URL(route.request().url());
  if (url.pathname.endsWith("/status/stream"))
    return route.fulfill({ status: 200, contentType: "text/event-stream", body: ": ok\n\n" });
  const path = url.pathname.replace(/^\/api\/v1/, "");
  const hit = ROUTES.find(([re]) => re.test(path));
  if (hit) return route.fulfill({ json: hit[1] });
  return route.fulfill({ json: page([]) });
}

export async function mockApi(p: Page): Promise<void> {
  await p.route("**/api/v1/**", mock);
}

export const test = base.extend<{ api: void }>({
  api: [
    async ({ page: p }, use) => {
      await mockApi(p);
      await use();
    },
    { auto: true },
  ],
});
export { expect } from "@playwright/test";
