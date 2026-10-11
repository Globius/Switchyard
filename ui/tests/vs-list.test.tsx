import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { expiryStatus } from "@/components/sy/StatusPill";
import { metric } from "@/features/vs/VirtualServicesPage";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const NOW = Date.now();
const days = (n: number) => new Date(NOW + n * 86_400_000 + 3_600_000).toISOString();

const ME = (role: string) => ({
  auth: "session",
  break_glass: false,
  csrf_token: "tok",
  email: "m@example.test",
  id: "s1",
  is_system: false,
  name: "Miguel Rodriguez",
  principal: "Miguel Rodriguez",
  session: null,
  tenants: [{ id: "t1", name: "acme", role, via: [] }],
});
const vs = (o: object) => ({
  id: "v0",
  name: "x",
  tenant_id: "t1",
  version: 1,
  protocol: "http",
  vip: "10.0.0.1",
  vip_pool_id: null,
  se_group_id: "g1",
  ports: [{ port: 443, tls: true, http2: true, proxy_protocol: false, https_redirect_port: null }],
  certificate_ids: [],
  enabled: true,
  routes: [],
  policy_set_ids: [],
  enforce_allow_list: false,
  draft_status: "unchanged",
  changed_fields: [],
  ...o,
});

let role = "operator";
let list: object[] = [];
let metricsAvailable = false;
const server = setupServer(
  http.get("*/api/v1/me", () => HttpResponse.json(ME(role))),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "write", object: "application", min_role: "operator", description: "Change apps." },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services", ({ request }) => {
    const q = new URL(request.url).searchParams.get("q");
    return HttpResponse.json({
      items: q ? list.filter((v) => (v as { name: string }).name.includes(q)) : list,
      next_cursor: null,
    });
  }),
  http.get("*/api/v1/tenants/acme/status/virtual-services", () =>
    HttpResponse.json([
      {
        id: "v1",
        name: "web",
        protocol: "http",
        state: "serving",
        health: "healthy",
        members_total: 2,
        members_up: 2,
      },
      {
        id: "v2",
        name: "api",
        protocol: "http",
        state: "serving",
        health: "degraded",
        members_total: 2,
        members_up: 1,
      },
    ]),
  ),
  http.get("*/api/v1/tenants/acme/metrics/virtual-services", () =>
    HttpResponse.json(
      metricsAvailable
        ? {
            available: true,
            reason: null,
            window_s: 900,
            items: [
              {
                name: "web",
                virtual_service_id: "v1",
                summary: { rps: 12.34, p95_ms: 18, error_5xx_ratio: 0.002, active_connections: 3 },
              },
            ],
          }
        : {
            available: false,
            reason: "no metrics backend is configured",
            window_s: 900,
            items: [],
          },
    ),
  ),
  http.get("*/api/v1/tenants/acme/se-groups", () =>
    HttpResponse.json({ items: [{ id: "g1", name: "se-edge-a" }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/certificates", () =>
    HttpResponse.json({
      items: [
        { id: "c1", not_after: days(41) },
        { id: "c2", not_after: days(5) },
      ],
      next_cursor: null,
    }),
  ),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  role = "operator";
  metricsAvailable = false;
});
afterAll(() => server.close());

function renderAt(path: string) {
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>,
  );
  return router;
}

describe("helpers", () => {
  it("never shows 0 for a missing metric", () => {
    expect(metric(null)).toBe("—");
    expect(metric(undefined, 1)).toBe("—");
    expect(metric(0, 1)).toBe("0.0");
    expect(metric(18, 0, " ms")).toBe("18 ms");
  });
  it("expiry thresholds", () => {
    expect(expiryStatus(days(41), NOW).status).toBe("neutral");
    expect(expiryStatus(days(20), NOW).status).toBe("degraded");
    expect(expiryStatus(days(5), NOW).status).toBe("down");
    expect(expiryStatus(days(-1), NOW).label).toBe("Expired");
  });
});

describe("virtual services list", () => {
  it("shows rows with health, SE group, cert expiry and an unavailable-metrics note", async () => {
    list = [
      vs({ id: "v1", name: "web", certificate_ids: ["c1"] }),
      vs({ id: "v2", name: "api", certificate_ids: ["c1", "c2"] }),
      vs({ id: "v3", name: "docs", draft_status: "created", vip: null, ports: [{ port: 80 }] }),
      vs({ id: "v4", name: "old", draft_status: "deleted" }),
      vs({ id: "v5", name: "off", enabled: false }),
    ];
    renderAt("/t/acme/virtual-services");
    const web = (await screen.findByRole("link", { name: "web" })).closest("tr")!;
    expect(within(web).getByText("Healthy")).toBeInTheDocument();
    expect(within(web).getByText("se-edge-a")).toBeInTheDocument();
    expect(within(web).getByText("41 days")).toBeInTheDocument();
    const api = screen.getByRole("link", { name: "api" }).closest("tr")!;
    expect(within(api).getByText("Degraded")).toBeInTheDocument();
    expect(within(api).getByText("5 days")).toBeInTheDocument();
    const docs = screen.getByRole("link", { name: "docs" }).closest("tr")!;
    expect(within(docs).getByText("New")).toBeInTheDocument();
    expect(within(docs).getByText("Not applied")).toBeInTheDocument();
    expect(within(docs).getByText("allocated on apply")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "old" })).toHaveClass("line-through");
    expect(
      within(screen.getByRole("link", { name: "off" }).closest("tr")!).getByText("Disabled"),
    ).toBeInTheDocument();
    expect((await screen.findByText(/Metrics unavailable/)).closest("div")).toHaveTextContent(
      "no metrics backend",
    );
    expect(within(web).getAllByText("—").length).toBeGreaterThanOrEqual(3);
  });

  it("shows metrics when the backend has them", async () => {
    metricsAvailable = true;
    list = [vs({ id: "v1", name: "web" })];
    renderAt("/t/acme/virtual-services");
    const web = (await screen.findByRole("link", { name: "web" })).closest("tr")!;
    await waitFor(() => expect(within(web).getByText("12.3")).toBeInTheDocument());
    expect(within(web).getByText("18 ms")).toBeInTheDocument();
    expect(within(web).getByText("0.20%")).toBeInTheDocument();
    expect(screen.queryByText(/Metrics unavailable/)).not.toBeInTheDocument();
  });

  it("empty state offers the create action to operators and only text to viewers", async () => {
    list = [];
    renderAt("/t/acme/virtual-services");
    expect(await screen.findByText("No virtual services yet")).toBeInTheDocument();
    expect(
      await screen.findByRole("link", { name: /Create a virtual service/ }),
    ).toBeInTheDocument();
  });

  it("viewer sees the empty text without the button and a disabled New button", async () => {
    role = "viewer";
    list = [];
    renderAt("/t/acme/virtual-services");
    expect(await screen.findByText("No virtual services yet")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /Create a virtual service/ }),
    ).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /New virtual service/ })).toBeDisabled(),
    );
  });

  it("filters by name through the URL", async () => {
    list = [vs({ id: "v1", name: "web" }), vs({ id: "v2", name: "api" })];
    const router = renderAt("/t/acme/virtual-services");
    await screen.findByRole("link", { name: "web" });
    await userEvent.type(screen.getByLabelText("Filter by name"), "api");
    await waitFor(() => expect(router.state.location.search).toMatchObject({ q: "api" }));
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "web" })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("link", { name: "api" })).toBeInTheDocument();
  });

  it("explains an empty filtered result and shows API errors with Retry", async () => {
    list = [];
    renderAt("/t/acme/virtual-services?q=zzz");
    expect(await screen.findByText("No virtual services match")).toBeInTheDocument();
  });

  it("shows the problem title and detail on error", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/virtual-services", () =>
        HttpResponse.json(
          { title: "Service unavailable", status: 503, detail: "The database is down." },
          { status: 503, headers: { "content-type": "application/problem+json" } },
        ),
      ),
    );
    renderAt("/t/acme/virtual-services");
    expect(await screen.findByRole("alert", {}, { timeout: 8000 })).toHaveTextContent(
      "The database is down.",
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  }, 15000);

  it("has an Enabled switch that adds an update op to the draft", async () => {
    list = [vs({ id: "v1", name: "web", version: 7 })];
    let ops: {
      ops: { action: string; object_id: string; body: unknown; expected_version: number }[];
    } | null = null;
    server.use(
      http.post("*/api/v1/tenants/acme/change-sets", () =>
        HttpResponse.json(
          {
            id: "cs1",
            state: "draft",
            author: "Miguel Rodriguez",
            summary: "x",
            base_revision: 1,
            created_at: "2026-10-10T10:00:00Z",
            ops: [],
          },
          { status: 201 },
        ),
      ),
      http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", async ({ request }) => {
        ops = (await request.json()) as typeof ops;
        return HttpResponse.json({ id: "cs1" });
      }),
    );
    renderAt("/t/acme/virtual-services");
    const sw = await screen.findByRole("switch", { name: "Enable web" });
    await waitFor(() => expect(sw).toBeEnabled());
    expect(sw).toHaveAttribute("aria-checked", "true");
    await userEvent.click(sw);
    await waitFor(() =>
      expect(ops?.ops[0]).toMatchObject({
        action: "update",
        object_id: "v1",
        expected_version: 7,
        body: { enabled: false },
      }),
    );
  });

  it("Restore removes the draft's delete op for a deleted service", async () => {
    list = [vs({ id: "v9", name: "legacy", draft_status: "deleted" })];
    let removed: number | null = null;
    server.use(
      http.get("*/api/v1/tenants/acme/change-sets", ({ request }) =>
        HttpResponse.json({
          items:
            new URL(request.url).searchParams.get("state") === "draft"
              ? [
                  {
                    id: "cs1",
                    state: "draft",
                    author: "Miguel Rodriguez",
                    summary: "d",
                    base_revision: 1,
                    created_at: "2026-10-10T10:00:00Z",
                    op_count: 2,
                  },
                ]
              : [],
          next_cursor: null,
        }),
      ),
      http.get("*/api/v1/tenants/acme/change-sets/cs1", () =>
        HttpResponse.json({
          id: "cs1",
          state: "draft",
          author: "Miguel Rodriguez",
          summary: "d",
          base_revision: 1,
          created_at: "2026-10-10T10:00:00Z",
          approver: null,
          reject_comment: null,
          error: null,
          ops: [
            {
              action: "update",
              object_type: "pool",
              object_id: "p",
              expected_version: 1,
              body: {},
            },
            {
              action: "delete",
              object_type: "virtual_service",
              object_id: "v9",
              expected_version: 1,
              body: null,
            },
          ],
        }),
      ),
      http.delete("*/api/v1/tenants/acme/change-sets/cs1/ops/:index", ({ params }) => {
        removed = Number(params.index);
        return HttpResponse.json({});
      }),
    );
    renderAt("/t/acme/virtual-services");
    const btn = await screen.findByRole("button", { name: "Restore" });
    await waitFor(() => expect(btn).toBeEnabled());
    await userEvent.click(btn);
    await waitFor(() => expect(removed).toBe(1));
  });

  it("viewers see the switch disabled", async () => {
    role = "viewer";
    list = [vs({ id: "v1", name: "web" })];
    renderAt("/t/acme/virtual-services");
    const sw = await screen.findByRole("switch", { name: "Enable web" });
    await waitFor(() => expect(sw).toBeDisabled());
  });
});
