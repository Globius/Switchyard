// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Demo dataset for the docs screenshots, layered over the test mock in `fixtures.ts`: a tenant with
 * a handful of services, pools, engines, certificates and change sets, so screens are not empty.
 * Addresses are documentation ranges (RFC 5737, RFC 3849); nothing here comes from a real estate.
 */
import type { Page } from "@playwright/test";
import { VS } from "./fixtures";

const page = (items: unknown[]) => ({ items, next_cursor: null });
const obj = { version: 2, draft_status: "unchanged", changed_fields: [], tenant_id: "t1" };
const NOW = Date.now();
const iso = (minutesAgo: number) => new Date(NOW - minutesAgo * 60_000).toISOString();
const inDays = (d: number) => new Date(NOW + d * 86_400_000).toISOString();

const port = (p: number, tls = false) => ({
  port: p,
  tls,
  http2: tls,
  proxy_protocol: false,
  https_redirect_port: p === 80 ? 443 : null,
});
const service = (id: string, name: string, vip: string, pool: string, extra: object = {}) => ({
  ...VS,
  id,
  name,
  vip,
  default_pool_id: pool,
  routes: [],
  certificate_ids: ["c1"],
  ports: [port(80), port(443, true)],
  ...extra,
});
const SERVICES = [
  { ...VS, vip: "192.0.2.10", certificate_ids: ["c1"] },
  service("v2", "shop", "192.0.2.11", "p2"),
  service("v3", "api", "192.0.2.12", "p3", { se_group_id: "g1" }),
  service("v4", "grafana", "192.0.2.20", "p4", { se_group_id: "g2" }),
  service("v5", "dns", "192.0.2.53", "p5", {
    protocol: "udp",
    se_group_id: "g2",
    ports: [{ ...port(53), https_redirect_port: null }],
    certificate_ids: [],
  }),
];
const STATUS = [
  { id: "v1", health: "healthy", members_up: 3, members_total: 3 },
  { id: "v2", health: "degraded", members_up: 2, members_total: 3 },
  { id: "v3", health: "healthy", members_up: 4, members_total: 4 },
  { id: "v4", health: "healthy", members_up: 1, members_total: 1 },
  { id: "v5", health: "healthy", members_up: 2, members_total: 2 },
];

const member = (id: string, address: string, extra: object = {}) => ({
  id,
  name: null,
  address,
  port: 8080,
  weight: 100,
  admin_state: "enabled",
  backup: false,
  ...extra,
});
const pool = (id: string, name: string, members: unknown[], extra: object = {}) => ({
  id,
  name,
  algorithm: "roundrobin",
  backend_protocol: "http1",
  health_monitor_id: "m1",
  tls: false,
  tls_verify: false,
  members,
  ...obj,
  ...extra,
});
const POOLS = [
  pool("p1", "app", [
    member("a1", "198.51.100.11"),
    member("a2", "198.51.100.12"),
    member("a3", "198.51.100.13"),
  ]),
  pool("p2", "shop", [
    member("s1", "198.51.100.21"),
    member("s2", "198.51.100.22"),
    member("s3", "198.51.100.23", { admin_state: "drain" }),
  ]),
  pool(
    "p3",
    "api",
    ["31", "32", "33", "34"].map((n) => member(`i${n}`, `198.51.100.${n}`, { port: 9000 })),
    { algorithm: "leastconn" },
  ),
  pool("p4", "grafana", [member("g1", "198.51.100.41", { port: 3000 })]),
  pool(
    "p5",
    "dns",
    [member("d1", "198.51.100.53", { port: 53 }), member("d2", "198.51.100.54", { port: 53 })],
    {
      health_monitor_id: null,
    },
  ),
];

const GROUPS = [
  {
    id: "g1",
    name: "edge",
    ha_mode: "bgp",
    maxconn: 50000,
    canary_seconds: 30,
    routing_profile_id: null,
    networks: [],
    peers: null,
    ...obj,
  },
  {
    id: "g2",
    name: "internal",
    ha_mode: "vrrp",
    maxconn: 20000,
    canary_seconds: 30,
    routing_profile_id: null,
    networks: [],
    peers: null,
    ...obj,
  },
];
const engine = (id: string, name: string, group: string, node: string, ip: string) => ({
  id,
  name,
  se_group_id: group,
  state: "enabled",
  url: `https://${ip}:5556`,
  mgmt_ip: ip,
  agent_enrolled: true,
  guest_kind: "qemu",
  host_online: true,
  pve_node: node,
  vmid: 900 + Number(id.slice(1)),
});
const ENGINES = [
  engine("e1", "edge-1", "g1", "pve1", "203.0.113.11"),
  engine("e2", "edge-2", "g1", "pve2", "203.0.113.12"),
  engine("e3", "edge-3", "g1", "pve2", "203.0.113.13"),
  engine("e4", "internal-1", "g2", "pve1", "203.0.113.21"),
  engine("e5", "internal-2", "g2", "pve2", "203.0.113.22"),
];
const HEALTH = [
  {
    se_group_id: "g1",
    name: "edge",
    score: 100,
    reasons: [],
    engines: ENGINES.slice(0, 3).map((e) => ({
      engine_id: e.id,
      engine: e.name,
      score: 100,
      reasons: [],
    })),
  },
  {
    se_group_id: "g2",
    name: "internal",
    score: 100,
    reasons: [],
    engines: ENGINES.slice(3).map((e) => ({
      engine_id: e.id,
      engine: e.name,
      score: 100,
      reasons: [],
    })),
  },
];

const ENGINE_STATUS = ENGINES.map((e) => ({
  engine_id: e.id,
  engine: e.name,
  se_group_id: e.se_group_id,
  reachable: true,
  observed_at: iso(0),
  drift: [],
  error: null,
  members: POOLS.flatMap((p) =>
    p.members.map((m) => ({
      backend: `pool_${p.name}`,
      server: m.id,
      status: m.admin_state === "drain" ? "DRAIN" : "UP",
      check_status: "L7OK",
      last_change_s: 86_400,
    })),
  ),
}));

const cert = (id: string, name: string, sans: string[], days: number, source: string) => ({
  id,
  name,
  sans,
  not_after: inDays(days),
  source,
  renew_policy: source === "upload" ? "manual" : "auto",
  renew_before_days: 30,
  purpose: "server",
  ocsp_stapling: false,
  secret_ref: `kv/tenants/acme/certs/${name}`,
  ...obj,
});
const CERTS = [
  cert("c1", "app-example-com", ["app.example.com"], 74, "acme"),
  cert("c2", "shop-example-com", ["shop.example.com", "www.shop.example.com"], 12, "acme"),
  cert("c3", "wildcard-internal", ["*.internal.example.com"], 301, "upload"),
];

const CHANGE_SETS = [
  {
    id: "cs1",
    summary: "Move api to leastconn and add a fourth member",
    author: "dana",
    state: "draft",
    base_revision: 41,
    created_at: iso(12),
    op_count: 2,
  },
  {
    id: "cs2",
    summary: "Rate-limit /login on shop",
    author: "sam",
    state: "pending_approval",
    base_revision: 41,
    created_at: iso(55),
    op_count: 2,
  },
  {
    id: "cs3",
    summary: "Renew shop-example-com",
    author: "system:acme",
    state: "applied",
    base_revision: 40,
    created_at: iso(240),
    op_count: 1,
  },
];
const REVISIONS = [
  ["41", "Add grafana behind the internal group", "dana", 300],
  ["40", "Renew shop-example-com", "system:acme", 1440],
  ["39", "Enable HTTP/3 on web", "sam", 2900],
  ["38", "WAF on shop: block mode", "dana", 4400],
  ["37", "Discovery: api members from tag sy-acme.api", "system:discovery", 6000],
].map(([n, summary, author, ago]) => ({
  number: Number(n),
  summary,
  author,
  created_at: iso(Number(ago)),
  git_sha: null,
}));
const DIFF = [
  "@@ backend pool_api @@",
  "-  balance roundrobin",
  "+  balance leastconn",
  "   server i31 198.51.100.31:9000 check",
  "   server i32 198.51.100.32:9000 check",
  "   server i33 198.51.100.33:9000 check",
  "+  server i34 198.51.100.34:9000 check",
].join("\n");

const ROUTES: [RegExp, unknown][] = [
  [/\/status\/virtual-services$/, STATUS],
  [/\/status\/engines$/, ENGINE_STATUS],
  [/\/status\/se-groups$/, HEALTH],
  [/\/virtual-services$/, page(SERVICES)],
  [/\/virtual-services\/v1$/, SERVICES[0]],
  [/\/certificates\/c1$/, CERTS[0]],
  [
    /\/policy-sets$/,
    page([
      {
        id: "ps1",
        name: "shop-waf",
        kind: "waf",
        mode: "block",
        crs_version: "4.8.0",
        paranoia_level: 1,
        inbound_threshold: 5,
        outbound_threshold: 4,
        disabled_rule_ids: [920350],
        excluded_paths: ["/healthz"],
        inspect_body: true,
        fail_mode: "open",
        timeout_ms: 100,
        ...obj,
      },
      {
        id: "ps2",
        name: "login-limit",
        kind: "rate_limit",
        rules: [
          {
            match: { path_prefix: "/login" },
            key: "client_ip",
            max_requests: 10,
            period_s: 60,
            action: "deny",
            deny_status: 429,
            tarpit_s: 5,
            table_size: 10000,
          },
        ],
        ...obj,
      },
      {
        id: "ps3",
        name: "office-only",
        kind: "allow_list",
        cidrs: ["198.51.100.0/24", "2001:db8:100::/48"],
        managed_by: null,
        countries: [],
        feed: null,
        ...obj,
      },
    ]),
  ],
  [/\/pools$/, page(POOLS)],
  [
    /\/health-monitors$/,
    page([
      {
        id: "m1",
        name: "http-healthz",
        type: "http",
        port: null,
        interval_s: 5,
        timeout_s: 3,
        rise: 2,
        fall: 3,
        http: { method: "GET", path: "/healthz", host: null, expect_status: [200] },
        udp: null,
        preset: null,
        ...obj,
      },
    ]),
  ],
  [/\/se-groups$/, page(GROUPS)],
  [/\/engines$/, ENGINES],
  [/\/certificate-renewals$/, []],
  [/\/certificates$/, page(CERTS)],
  [
    /\/change-sets$/,
    (q: URLSearchParams) =>
      page(
        CHANGE_SETS.filter((c) => !q.getAll("state").length || q.getAll("state").includes(c.state)),
      ),
  ],
  [
    /\/change-sets\/cs1$/,
    {
      ...CHANGE_SETS[0],
      ops: [
        { action: "update", object_type: "pool", object_id: "p3", expected_version: 2 },
        { action: "update", object_type: "pool", object_id: "p3", expected_version: 2 },
      ],
    },
  ],
  [
    /\/change-sets\/cs1\/diff$/,
    {
      objects: [
        {
          id: "p3",
          name: "api",
          type: "pool",
          status: "modified",
          changed_fields: ["algorithm", "members"],
        },
      ],
      se_groups: [
        {
          id: "g1",
          name: "edge",
          error: null,
          files: [
            {
              path: "haproxy/haproxy.cfg",
              status: "modified",
              diff: DIFF,
              before: null,
              after: null,
            },
          ],
        },
      ],
      impact: {
        services: [{ id: "v3", name: "api", se_group_id: "g1", vip: "192.0.2.12" }],
        se_groups: [{ id: "g1", name: "edge" }],
        vips: ["192.0.2.12"],
        hostnames: ["api.example.com"],
      },
      warnings: [],
      problems: [],
    },
  ],
  [/\/revisions$/, page(REVISIONS)],
  [
    /\/summary$/,
    {
      services: { total: 5, healthy: 4, degraded: 1, down: 0, disabled: 0, unknown: 0 },
      failing_members: [
        {
          backend: "pool_shop",
          server: "s3",
          engine: "edge-2",
          engine_id: "e2",
          status: "DRAIN",
          check_status: "L7OK",
          last_check: "HTTP 200",
          last_change_s: 840,
          last_checked_at: iso(1),
        },
      ],
      drifted_engines: [],
      unreachable_engines: [],
      expiring_certificates: [
        { id: "c2", name: "shop-example-com", days_left: 12, not_after: inDays(12) },
      ],
      jobs: { running: 1, queued: 0, failed: 0 },
      open_drafts: 1,
      pending_approval: 1,
      latest_revision: {
        number: 41,
        summary: REVISIONS[0].summary,
        author: "dana",
        created_at: iso(300),
      },
    },
  ],
];

/** Serve the demo dataset; anything it does not cover falls through to the test mock. */
export async function mockDemo(p: Page): Promise<void> {
  await p.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api\/v1/, "");
    const hit = ROUTES.find(([re]) => re.test(path));
    if (hit)
      return route.fulfill({
        json: typeof hit[1] === "function" ? hit[1](url.searchParams) : hit[1],
      });
    return route.fallback();
  });
}
