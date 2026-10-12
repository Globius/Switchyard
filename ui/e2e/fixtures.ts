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

/** 120 points over the last hour with a gap in the connections series. */
function series() {
  const end = Date.parse("2026-10-11T11:00:00Z");
  const at = (i: number) => new Date(end - (119 - i) * 30_000).toISOString();
  const line = (base: number, amp: number, skip = false) =>
    Array.from({ length: 120 }, (_, i) => i)
      .filter((i) => !(skip && i > 50 && i < 62))
      .map((i) => ({ t: at(i), v: base + amp * Math.sin(i / 9) + (i % 7) }));
  return {
    available: true,
    reason: null,
    metrics: {
      window_s: 3600,
      step_s: 30,
      summary: { rps: 379, p95_ms: 43, error_5xx_ratio: 0.005, active_connections: 1504 },
      rps: line(420, 90),
      p95_ms: line(50, 8),
      error_5xx_ratio: line(0.005, 0.003).map((p) => ({ ...p, v: Math.abs(p.v) / 100 })),
      active_connections: line(1300, 200, true),
    },
  };
}

export const VS = {
  id: "v1",
  tenant_id: "t1",
  name: "web",
  enabled: true,
  protocol: "http",
  se_group_id: "g1",
  vip: "2001:0db8:abcd:0012:0000:0000:0000:006f",
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
  [
    /\/tenants\/acme$/,
    {
      id: "t1",
      name: "acme",
      version: 1,
      members: 3,
      require_approval: false,
      quotas: { virtual_service: 10, pool: 4 },
      usage: { virtual_service: 9, pool: 4 },
    },
  ],
  [/\/quota-requests$/, []],
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
  [/\/virtual-services\/[^/]+\/metrics$/, series()],
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
