import { QueryClientProvider } from "@tanstack/react-query";
import { pick } from "./pick";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { describeCheck, emptyMonitor, toForm as monitorForm } from "@/features/monitors/Monitors";
import { emptyPool, persistenceLabel, poolPatch, toForm } from "@/features/pools/Pools";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const member = (id: string, address: string, admin_state = "enabled") => ({
  id,
  name: null,
  address,
  port: 8080,
  weight: 100,
  admin_state,
});
const POOL = {
  id: "p1",
  name: "api-v2",
  algorithm: "roundrobin",
  health_monitor_id: "m1",
  tls: false,
  tls_verify: false,
  persistence: null,
  version: 4,
  draft_status: "unchanged",
  changed_fields: [],
  tenant_id: "t1",
  members: [member("a", "10.0.0.1"), member("b", "10.0.0.2", "drain")],
};
const MON = {
  id: "m1",
  name: "http-healthz",
  type: "http",
  interval_s: 5,
  timeout_s: 2,
  rise: 2,
  fall: 3,
  port: null,
  http: { method: "GET", path: "/healthz", host: null, expect_status: [200, 204] },
  udp: null,
  version: 2,
  draft_status: "unchanged",
  changed_fields: [],
  tenant_id: "t1",
};

describe("pool and monitor helpers", () => {
  it("builds patches of changed fields only", () => {
    const base = toForm(POOL as never);
    expect(poolPatch(base, base)).toEqual({});
    expect(Object.keys(poolPatch(base, { ...base, algorithm: "leastconn", tls: true }))).toEqual([
      "algorithm",
      "tls",
    ]);
    expect(emptyPool().members).toEqual([]);
    expect(persistenceLabel({ type: "source_ip", table_size: 1, timeout_s: 1800 })).toBe(
      "Source IP · 30 min",
    );
    expect(persistenceLabel(null)).toBe("—");
  });
  it("describes checks", () => {
    expect(describeCheck(monitorForm(MON as never))).toBe("GET /healthz → 200, 204");
    expect(describeCheck({ type: "tcp", http: null, udp: null })).toBe("TCP connect");
    expect(
      describeCheck({ type: "udp", http: null, udp: { payload_hex: null, require_reply: false } }),
    ).toBe("UDP probe, no reply required");
    expect(emptyMonitor().type).toBe("tcp");
  });
});

let role = "operator";
let posted: {
  ops: {
    action: string;
    object_type: string;
    object_id: string;
    body?: Record<string, unknown>;
    expected_version?: number;
  }[];
} | null = null;
let hasDraft = true;
let testBody: unknown = null;
const server = setupServer(
  http.get("*/api/v1/me", () =>
    HttpResponse.json({
      auth: "session",
      break_glass: false,
      csrf_token: "tok",
      email: null,
      id: "s",
      is_system: false,
      name: "Me",
      principal: "me",
      session: null,
      tenants: [{ id: "t1", name: "acme", role, via: [] }],
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "write", object: "application", min_role: "operator", description: "Change apps." },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/change-sets", ({ request }) =>
    HttpResponse.json({
      items:
        hasDraft && new URL(request.url).searchParams.get("state") === "draft"
          ? [
              {
                id: "cs1",
                state: "draft",
                author: "me",
                summary: "d",
                base_revision: 1,
                created_at: "2026-10-10T10:00:00Z",
                op_count: 1,
              },
            ]
          : [],
      next_cursor: null,
    }),
  ),
  http.post("*/api/v1/tenants/acme/change-sets", () => {
    hasDraft = true;
    return HttpResponse.json(
      {
        id: "cs1",
        state: "draft",
        author: "me",
        summary: "x",
        base_revision: 1,
        created_at: "2026-10-10T10:00:00Z",
        ops: [],
      },
      { status: 201 },
    );
  }),
  http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", async ({ request }) => {
    posted = (await request.json()) as typeof posted;
    return HttpResponse.json({ id: "cs1" });
  }),
  http.get("*/api/v1/tenants/acme/pools", () =>
    HttpResponse.json({
      items: [
        POOL,
        {
          ...POOL,
          id: "p2",
          name: "api-v1",
          algorithm: "leastconn",
          health_monitor_id: "m1",
          members: [],
          persistence: { type: "source_ip", table_size: 100000, timeout_s: 3600 },
          tls: true,
          tls_verify: true,
          draft_status: "created",
        },
      ],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/tenants/acme/health-monitors", () =>
    HttpResponse.json({
      items: [MON, { ...MON, id: "m2", name: "tcp-basic", type: "tcp", http: null }],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services", () =>
    HttpResponse.json({
      items: [
        {
          id: "v1",
          name: "api-gateway",
          default_pool_id: "p1",
          routes: [{ order: 1, match: {}, pool_id: "p1", redirect: null }],
        },
      ],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/tenants/acme/engines", () =>
    HttpResponse.json([{ id: "e1", name: "se-a-01" }]),
  ),
  http.post("*/api/v1/tenants/acme/health-monitors/m1/test", async ({ request }) => {
    testBody = await request.json();
    return HttpResponse.json({
      ok: false,
      detail: "connection timed out after 2 s",
      engine: "se-a-01",
      latency_ms: 2003,
      protocol: "http",
      target: "10.0.0.1:8080",
    });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  role = "operator";
  posted = null;
  hasDraft = true;
  testBody = null;
});
afterAll(() => server.close());

function renderAt(path: string) {
  render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider
          router={createRouter({
            routeTree,
            history: createMemoryHistory({ initialEntries: [path] }),
          })}
        />
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("pools", () => {
  it("lists pools with monitor, persistence, backend TLS, draining and used by", async () => {
    renderAt("/t/acme/pools");
    const row = (await screen.findByRole("button", { name: "api-v2" })).closest("tr")!;
    expect(within(row).getByText("2 members")).toBeInTheDocument();
    expect(within(row).getByText("1 draining")).toBeInTheDocument();
    expect(within(row).getByText("http-healthz")).toBeInTheDocument();
    expect(within(row).getByText("api-gateway")).toBeInTheDocument();
    const other = screen.getByRole("button", { name: "api-v1" }).closest("tr")!;
    expect(within(other).getByText("New")).toBeInTheDocument();
    expect(within(other).getByText("Source IP · 60 min")).toBeInTheDocument();
    expect(within(other).getByText("On · verify")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Filter by name"), "v2");
    expect(screen.queryByRole("button", { name: "api-v1" })).not.toBeInTheDocument();
  });

  it("edits a pool and saves only the changed fields as an update op with the version", async () => {
    renderAt("/t/acme/pools");
    await userEvent.click(await screen.findByRole("button", { name: "api-v2" }));
    const algo = await screen.findByLabelText("Algorithm");
    await waitFor(() => expect(algo).toBeEnabled());
    expect(screen.getByRole("button", { name: "Save to draft" })).toBeDisabled();
    await userEvent.selectOptions(algo, "leastconn");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() =>
      expect(posted?.ops[0]).toMatchObject({
        action: "update",
        object_type: "pool",
        object_id: "p1",
        expected_version: 4,
        body: { algorithm: "leastconn" },
      }),
    );
  });

  it("sets a passive check and slow start on a pool that has a health monitor", async () => {
    renderAt("/t/acme/pools");
    await userEvent.click(await screen.findByRole("button", { name: "api-v2" }));
    const box = await screen.findByLabelText("Judge members by their real traffic");
    await waitFor(() => expect(box).toBeEnabled());
    await userEvent.click(box);
    await userEvent.selectOptions(screen.getByLabelText("Observe"), "layer4");
    const limit = screen.getByLabelText("Error limit");
    await userEvent.clear(limit);
    await userEvent.type(limit, "5");
    await userEvent.selectOptions(screen.getByLabelText("On error"), "sudden-death");
    await userEvent.type(screen.getByLabelText("Slow start"), "60");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() =>
      expect(posted?.ops[0]?.body).toEqual({
        passive_check: { observe: "layer4", error_limit: 5, on_error: "sudden-death" },
        slow_start_s: 60,
      }),
    );
  });

  it("creates a pool with members and persistence", async () => {
    hasDraft = false;
    renderAt("/t/acme/pools");
    const n = await screen.findByRole("button", { name: /New pool/ });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "docs-web");
    await pick(screen.getByLabelText("Health monitor"), "m2");
    await userEvent.click(screen.getByLabelText("Backend TLS"));
    await userEvent.click(screen.getByLabelText("TLS verify"));
    await userEvent.selectOptions(screen.getByLabelText("Persistence type"), "source_ip");
    await userEvent.clear(screen.getByLabelText("Persistence timeout"));
    await userEvent.type(screen.getByLabelText("Persistence timeout"), "600");
    await userEvent.type(screen.getByLabelText("Table size"), "0");
    await userEvent.click(screen.getByRole("button", { name: "Add member" }));
    await userEvent.type(screen.getByLabelText("Member 1 address"), "10.9.0.1");
    await userEvent.selectOptions(screen.getByLabelText("Member 1 state"), "drain");
    await userEvent.clear(screen.getByLabelText("Member 1 weight"));
    await userEvent.type(screen.getByLabelText("Member 1 weight"), "50");
    await userEvent.clear(screen.getByLabelText("Member 1 port"));
    await userEvent.type(screen.getByLabelText("Member 1 port"), "9090");
    await userEvent.type(screen.getByLabelText("Member 1 name"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.action).toBe("create"));
    const b = posted!.ops[0]!.body as {
      name: string;
      tls: boolean;
      tls_verify: boolean;
      health_monitor_id: string;
      persistence: { timeout_s: number };
      members: { address: string; weight: number; port: number; admin_state: string }[];
    };
    expect(b).toMatchObject({
      name: "docs-web",
      tls: true,
      tls_verify: true,
      health_monitor_id: "m2",
      persistence: { timeout_s: 600 },
    });
    expect(b.members[0]).toMatchObject({
      address: "10.9.0.1",
      weight: 50,
      port: 9090,
      admin_state: "drain",
    });
  });

  it("turns on discovery by Proxmox tag, locks the member list, and sends the discovery block", async () => {
    hasDraft = false;
    renderAt("/t/acme/pools");
    const n = await screen.findByRole("button", { name: /New pool/ });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "web");
    await userEvent.click(screen.getByRole("switch", { name: "Discover members from Proxmox" }));
    expect(screen.getByLabelText("Proxmox tag")).toHaveValue("sy-acme.");
    await userEvent.type(screen.getByLabelText("Proxmox tag"), "web");
    await userEvent.type(screen.getByLabelText("Network (CIDR)"), "2001:db8:20::/64");
    expect(screen.getByRole("button", { name: "Add member" })).toBeDisabled();
    expect(screen.getByText(/These members are managed by discovery/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() =>
      expect(posted?.ops[0]?.body).toMatchObject({
        name: "web",
        discovery: {
          source: "proxmox_tag",
          tag: "sy-acme.web",
          cidr: "2001:db8:20::/64",
          port: 8080,
          weight: 100,
        },
      }),
    );
  });

  it("shows the tag error on the tag field and clears discovery with null", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/change-sets/:cs/ops", () =>
        HttpResponse.json(
          {
            title: "Invalid",
            status: 422,
            errors: [
              {
                path: "discovery.tag",
                code: "discovery_tag_not_allowed",
                message: "The tag must start with sy-acme.",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    renderAt("/t/acme/pools");
    const n = await screen.findByRole("button", { name: /New pool/ });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "web");
    await userEvent.click(screen.getByRole("switch", { name: "Discover members from Proxmox" }));
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    expect(await screen.findByText("The tag must start with sy-acme.")).toBeInTheDocument();
  });

  it("sets cookie persistence, PROXY protocol, a client certificate and member options", async () => {
    hasDraft = false;
    server.use(
      http.get("*/api/v1/tenants/acme/certificates", () =>
        HttpResponse.json({
          items: [
            {
              id: "cert1",
              name: "mtls-client",
              purpose: "server",
              sans: [],
              not_after: "2027-01-01T00:00:00Z",
              source: "upload",
              renew_policy: "manual",
              renew_before_days: 30,
              draft_status: "unchanged",
              changed_fields: [],
              version: 1,
              tenant_id: "t",
              secret_ref: "x",
              ocsp_stapling: false,
            },
            {
              id: "ca1",
              name: "private-ca",
              purpose: "client_ca",
              sans: [],
              not_after: "2027-01-01T00:00:00Z",
              source: "upload",
              renew_policy: "manual",
              renew_before_days: 30,
              draft_status: "unchanged",
              changed_fields: [],
              version: 1,
              tenant_id: "t",
              secret_ref: "x",
              ocsp_stapling: false,
            },
          ],
          next_cursor: null,
        }),
      ),
    );
    renderAt("/t/acme/pools");
    const n = await screen.findByRole("button", { name: /New pool/ });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "secure");
    await userEvent.selectOptions(screen.getByLabelText("Persistence type"), "http_cookie");
    await userEvent.clear(screen.getByLabelText("Cookie name"));
    await userEvent.type(screen.getByLabelText("Cookie name"), "SRV");
    await userEvent.type(screen.getByLabelText("Cookie lifetime"), "3600");
    await userEvent.click(screen.getByLabelText("Secure"));
    await userEvent.selectOptions(screen.getByLabelText("PROXY protocol"), "v2");
    await userEvent.click(screen.getByLabelText("Backend TLS"));
    await pick(screen.getByLabelText("Client certificate"), "cert1");
    await userEvent.click(screen.getByRole("button", { name: "Add member" }));
    await userEvent.type(screen.getByLabelText("Member 1 address"), "10.9.0.1");
    await userEvent.click(screen.getByLabelText("Member 1 backup"));
    await userEvent.type(screen.getByLabelText("Member 1 max connections"), "50");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.action).toBe("create"));
    const b = posted!.ops[0]!.body as { members: Record<string, unknown>[] };
    expect(b).toMatchObject({
      send_proxy: "v2",
      client_certificate_id: "cert1",
      persistence: {
        type: "http_cookie",
        name: "SRV",
        mode: "insert",
        ttl_s: 3600,
        secure: true,
        http_only: true,
      },
    });
    expect(b.members[0]).toMatchObject({ backup: true, maxconn: 50 });
  });

  it("offers header and URI persistence with their own fields", async () => {
    hasDraft = false;
    renderAt("/t/acme/pools");
    const n = await screen.findByRole("button", { name: /New pool/ });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "sticky");
    await userEvent.selectOptions(screen.getByLabelText("Persistence type"), "header");
    await userEvent.type(screen.getByLabelText("Persistence header"), "X-Session");
    await userEvent.selectOptions(screen.getByLabelText("Persistence type"), "uri");
    await userEvent.selectOptions(screen.getByLabelText("URI source"), "url_param");
    await userEvent.type(screen.getByLabelText("URI parameter"), "sid");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.action).toBe("create"));
    expect(posted!.ops[0]!.body).toMatchObject({
      persistence: {
        type: "uri",
        source: "url_param",
        param: "sid",
        table_size: 100000,
        timeout_s: 1800,
      },
    });
  });

  it("sets the backend protocol and the tunnel timeout for gRPC and WebSocket pools", async () => {
    hasDraft = false;
    renderAt("/t/acme/pools");
    const n = await screen.findByRole("button", { name: /New pool/ });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "grpc");
    await userEvent.click(screen.getByLabelText("Backend TLS"));
    await userEvent.selectOptions(screen.getByLabelText("Backend protocol"), "h2");
    await userEvent.type(screen.getByLabelText("Tunnel timeout"), "7200");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.action).toBe("create"));
    expect(posted!.ops[0]!.body).toMatchObject({ backend_protocol: "h2", tunnel_timeout_s: 7200 });
  });

  it("maps 422 findings onto member fields and deletes after confirming", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", () =>
        HttpResponse.json(
          {
            title: "Invalid",
            status: 422,
            detail: "x",
            errors: [{ path: "members.0.address", code: "bad", message: "Not an IP address." }],
          },
          { status: 422 },
        ),
      ),
    );
    renderAt("/t/acme/pools");
    await userEvent.click(await screen.findByRole("button", { name: "api-v2" }));
    const addr = await screen.findByLabelText("Member 1 address");
    await waitFor(() => expect(addr).toBeEnabled());
    await userEvent.clear(addr);
    await userEvent.type(addr, "nope");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    expect(await screen.findByText("Not an IP address.")).toBeInTheDocument();
  });

  it("delete adds a delete op after confirming", async () => {
    renderAt("/t/acme/pools");
    await userEvent.click(await screen.findByRole("button", { name: "api-v2" }));
    const del = await screen.findByRole("button", { name: "Delete" });
    await waitFor(() => expect(del).toBeEnabled());
    await userEvent.click(del);
    await userEvent.click(await screen.findByRole("button", { name: "Add delete to draft" }));
    await waitFor(() =>
      expect(posted?.ops[0]).toMatchObject({
        action: "delete",
        object_id: "p1",
        expected_version: 4,
      }),
    );
  });

  it("viewers see disabled controls and empty state without a button", async () => {
    role = "viewer";
    renderAt("/t/acme/pools");
    const n = await screen.findByRole("button", { name: /New pool/ });
    await waitFor(() => expect(n).toBeDisabled());
  });
});

describe("health monitors", () => {
  it("lists monitors with what they check and which pools use them", async () => {
    renderAt("/t/acme/health-monitors");
    const row = (await screen.findByRole("button", { name: "http-healthz" })).closest("tr")!;
    expect(within(row).getByText("GET /healthz → 200, 204")).toBeInTheDocument();
    expect(within(row).getByText("5 s · 2 s")).toBeInTheDocument();
    expect(await within(row).findByText("api-v2, api-v1")).toBeInTheDocument();
    expect(
      within(screen.getByRole("button", { name: "tcp-basic" }).closest("tr")!).getByText("none"),
    ).toBeInTheDocument();
  });

  it("edits a monitor and runs a test against one member", async () => {
    renderAt("/t/acme/health-monitors");
    await userEvent.click(await screen.findByRole("button", { name: "http-healthz" }));
    const interval = await screen.findByLabelText("Interval");
    await waitFor(() => expect(interval).toBeEnabled());
    await userEvent.clear(interval);
    await userEvent.type(interval, "10");
    await userEvent.click(screen.getByRole("button", { name: /Remove 204/ }));
    await userEvent.selectOptions(screen.getByLabelText("Method"), "HEAD");
    await userEvent.type(screen.getByLabelText("Port"), "9000");
    await userEvent.type(screen.getByLabelText("Host header"), "x.test");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() =>
      expect(posted?.ops[0]).toMatchObject({
        action: "update",
        object_type: "health_monitor",
        expected_version: 2,
      }),
    );
    expect(Object.keys(posted!.ops[0]!.body!)).toEqual(
      expect.arrayContaining(["interval_s", "port", "http"]),
    );
  });

  it("test box posts the chosen member and shows the result", async () => {
    renderAt("/t/acme/health-monitors");
    await userEvent.click(await screen.findByRole("button", { name: "http-healthz" }));
    await userEvent.selectOptions(await screen.findByLabelText("Member to test"), "a");
    await userEvent.selectOptions(screen.getByLabelText("From engine"), "e1");
    await userEvent.click(screen.getByRole("button", { name: "Run test" }));
    expect(await screen.findByText(/connection timed out after 2 s/)).toBeInTheDocument();
    expect(testBody).toEqual({ pool_id: "p1", member_id: "a", engine_id: "e1" });
  });

  it("creates a UDP monitor with no rise count", async () => {
    renderAt("/t/acme/health-monitors");
    const n = await screen.findByRole("button", { name: /New monitor/ });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "wg-udp");
    await userEvent.click(screen.getByRole("radio", { name: "UDP" }));
    expect(screen.queryByLabelText("Rise")).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Probe payload (hex)"), "ab");
    await userEvent.click(screen.getByLabelText("Require a reply"));
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.action).toBe("create"));
    expect(posted!.ops[0]!.body).toMatchObject({
      type: "udp",
      udp: { payload_hex: "ab", require_reply: true },
      http: null,
    });
  });

  it("adds a body or header expectation to an HTTP monitor, and a protocol preset to a TCP one", async () => {
    renderAt("/t/acme/health-monitors");
    const n = await screen.findByRole("button", { name: /New monitor/ });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "redis-check");
    await userEvent.selectOptions(screen.getByLabelText("Protocol check"), "redis");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.action).toBe("create"));
    expect(posted!.ops[0]!.body).toMatchObject({ type: "tcp", preset: "redis", http: null });
  });

  it("checks the response body, or one header, but not both", async () => {
    renderAt("/t/acme/health-monitors");
    const n = await screen.findByRole("button", { name: /New monitor/ });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "body-check");
    await userEvent.click(screen.getByRole("radio", { name: "HTTP" }));
    await userEvent.selectOptions(screen.getByLabelText("Also check"), "header");
    await userEvent.type(screen.getByLabelText("Expected header name"), "X-Health");
    await userEvent.type(screen.getByLabelText("Expected header value"), "ok");
    await userEvent.selectOptions(screen.getByLabelText("Also check"), "body");
    expect(screen.queryByLabelText("Expected header name")).toBeNull();
    await userEvent.type(screen.getByLabelText("Expected body"), "status_ok");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.action).toBe("create"));
    const http = (posted!.ops[0]!.body as { http: Record<string, unknown> }).http;
    expect(http).toMatchObject({ expect_body: "status_ok", expect_header: null });
  });

  it("delete warns when pools still use the monitor", async () => {
    renderAt("/t/acme/health-monitors");
    const row = (await screen.findByRole("button", { name: "http-healthz" })).closest("tr")!;
    await within(row).findByText("api-v2, api-v1");
    await userEvent.click(within(row).getByRole("button", { name: "http-healthz" }));
    const del = await screen.findByRole("button", { name: "Delete" });
    await waitFor(() => expect(del).toBeEnabled());
    await userEvent.click(del);
    expect(await screen.findByText(/2 pools use this monitor/)).toBeInTheDocument();
  });
});
