import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { describeMatch } from "@/features/vs/VsOverview";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const VS = {
  id: "v1",
  name: "web",
  tenant_id: "t1",
  version: 3,
  protocol: "http",
  vip: "10.0.0.1",
  vip_pool_id: null,
  se_group_id: "g1",
  ports: [{ port: 443, tls: true, http2: true, proxy_protocol: false, https_redirect_port: null }],
  certificate_ids: [],
  enabled: true,
  default_pool_id: "p1",
  routes: [
    {
      order: 1,
      match: { host: "api.example.test", path_prefix: "/v2" },
      pool_id: "p2",
      redirect: null,
    },
    {
      order: 2,
      match: { host: "old.example.test" },
      pool_id: null,
      redirect: { location: "https://new.example.test", code: 301 },
    },
  ],
  policy_set_ids: [],
  enforce_allow_list: false,
  draft_status: "unchanged",
  changed_fields: [],
};
const pool = (id: string, name: string, members: object[]) => ({
  id,
  name,
  algorithm: "roundrobin",
  tls: false,
  tls_verify: false,
  version: 1,
  tenant_id: "t1",
  draft_status: "unchanged",
  changed_fields: [],
  members,
});
const member = (id: string, address: string, admin_state: string) => ({
  id,
  address,
  admin_state,
  name: null,
  port: 8080,
  weight: 1,
});

let role = "operator";
let stateBody: unknown = null;
let stateResult: object = { state: "drain", applied: ["se-a"], failed: {} };
let empty = false;
const server = setupServer(
  http.get("*/api/v1/me", () =>
    HttpResponse.json({
      auth: "session",
      break_glass: false,
      csrf_token: "tok",
      email: null,
      id: "s",
      is_system: false,
      name: "Miguel",
      session: null,
      tenants: [{ id: "t1", name: "acme", role, via: [] }],
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        {
          verb: "set_state",
          object: "member",
          min_role: "operator",
          description: "Change member state.",
        },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services/v1", () => HttpResponse.json(VS)),
  http.get("*/api/v1/tenants/acme/status/virtual-services", () =>
    HttpResponse.json([
      {
        id: "v1",
        name: "web",
        protocol: "http",
        state: "serving",
        health: "healthy",
        members_total: 3,
        members_up: 3,
      },
    ]),
  ),
  http.get("*/api/v1/tenants/acme/metrics/virtual-services", () =>
    HttpResponse.json({ available: false, reason: "no metrics backend", window_s: 900, items: [] }),
  ),
  http.get("*/api/v1/tenants/acme/pools", () =>
    HttpResponse.json({
      items: [
        pool("p1", "web-pool", [
          member("m1", "10.1.0.1", "enabled"),
          member("m2", "10.1.0.2", "drain"),
        ]),
        pool("p2", "api-v2", [member("m3", "10.2.0.1", "enabled")]),
      ],
      next_cursor: null,
    }),
  ),
  http.put("*/api/v1/tenants/acme/pools/p1/members/m1/state", async ({ request }) => {
    stateBody = await request.json();
    return HttpResponse.json(stateResult);
  }),
  http.get("*/api/v1/tenants/acme/virtual-services/v1/rendered", () =>
    HttpResponse.json({
      items: empty
        ? []
        : [
            {
              file: "haproxy/haproxy.cfg",
              language: "haproxy",
              fingerprint: "a".repeat(64),
              se_group_id: "g1",
              content: "global\nfrontend web\n    bind 10.0.0.1:443\nbackend p1\n",
              range: [2, 3],
            },
          ],
    }),
  ),
  http.get("*/api/v1/tenants/acme/revisions", () =>
    HttpResponse.json({
      items: [
        {
          number: 42,
          summary: "Raise maxconn",
          author: "dana",
          created_at: "2026-10-10T10:00:00Z",
          git_sha: null,
        },
      ],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/tenants/acme/audit-events", () =>
    HttpResponse.json({
      items: [
        {
          action: "member.state",
          actor: "miguel",
          at: "2026-10-10T11:00:00Z",
          object: "x",
          diff: null,
          request_id: null,
          tenant_id: "t1",
        },
      ],
      next_cursor: null,
    }),
  ),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  role = "operator";
  empty = false;
  stateBody = null;
  stateResult = { state: "drain", applied: ["se-a"], failed: {} };
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

describe("virtual service detail", () => {
  it("describes route matches", () => {
    expect(describeMatch({ host: "a", path_prefix: "/x" })).toBe("a /x*");
    expect(describeMatch({ path_exact: "/y" })).toBe("= /y");
    expect(describeMatch({})).toBe("any request");
  });

  it("overview shows the header, tabs, routes and pools", async () => {
    renderAt("/t/acme/virtual-services/v1");
    expect(await screen.findByRole("heading", { name: "web" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Rendered config" })).toBeInTheDocument();
    const routes = await screen.findByText("api.example.test /v2*");
    expect(within(routes.closest("tr")!).getByText("api-v2")).toBeInTheDocument();
    expect(screen.getByText("Redirect 301 to https://new.example.test")).toBeInTheDocument();
    expect(screen.getByText("Draining")).toBeInTheDocument();
    expect(await screen.findByText(/Metrics unavailable/)).toBeInTheDocument();
  });

  it("drains a member after confirming, and shows partial failure", async () => {
    stateResult = { state: "drain", applied: ["se-a"], failed: { "se-b": "agent unreachable" } };
    renderAt("/t/acme/virtual-services/v1");
    const row = (await screen.findByText("10.1.0.1:8080")).closest("li")!;
    await waitFor(() => expect(within(row).getByRole("button", { name: "Drain" })).toBeEnabled());
    await userEvent.click(within(row).getByRole("button", { name: "Drain" }));
    expect(await screen.findByText(/skips the change set/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Apply now" }));
    await waitFor(() => expect(stateBody).toEqual({ state: "drain" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("agent unreachable");
  });

  it("viewers see disabled member actions", async () => {
    role = "viewer";
    renderAt("/t/acme/virtual-services/v1");
    const row = (await screen.findByText("10.1.0.1:8080")).closest("li")!;
    await waitFor(() => expect(within(row).getByRole("button", { name: "Drain" })).toBeDisabled());
  });

  it("rendered tab highlights the fragment range and shows the fingerprint", async () => {
    renderAt("/t/acme/virtual-services/v1/rendered");
    expect(await screen.findByText("haproxy/haproxy.cfg")).toBeInTheDocument();
    expect(screen.getByText("aaaaaaaaaaaa")).toBeInTheDocument();
    const lit = document.querySelectorAll("[data-highlight]");
    expect(lit).toHaveLength(2);
  });

  it("rendered tab explains when nothing is rendered", async () => {
    empty = true;
    renderAt("/t/acme/virtual-services/v1/rendered");
    expect(await screen.findByText("Nothing is rendered")).toBeInTheDocument();
  });

  it("history lists revisions and audit events", async () => {
    renderAt("/t/acme/virtual-services/v1/history");
    expect(await screen.findByText("Raise maxconn")).toBeInTheDocument();
    expect(screen.getByText("member.state")).toBeInTheDocument();
  });

  it("history is empty for a new service", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/revisions", () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
      http.get("*/api/v1/tenants/acme/audit-events", () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
    );
    renderAt("/t/acme/virtual-services/v1/history");
    expect(await screen.findByText("No history yet")).toBeInTheDocument();
  });

  it("shows a TCP service without routes and a deleted draft struck through", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/virtual-services/v1", () =>
        HttpResponse.json({
          ...VS,
          protocol: "tcp",
          routes: [],
          default_pool_id: null,
          vip: null,
          enabled: false,
          draft_status: "deleted",
        }),
      ),
    );
    renderAt("/t/acme/virtual-services/v1");
    expect(await screen.findByText(/TCP services have no routes/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "web" })).toHaveClass("line-through");
    expect(screen.getByText("Deleted")).toBeInTheDocument();
    expect(screen.getByText("This service sends traffic to no pool yet.")).toBeInTheDocument();
  });

  it("shows the problem when the service cannot be loaded", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/virtual-services/v1", () =>
        HttpResponse.json(
          { title: "Not found", status: 404, detail: "No such virtual service." },
          { status: 404 },
        ),
      ),
    );
    renderAt("/t/acme/virtual-services/v1");
    expect(await screen.findByRole("alert")).toHaveTextContent("No such virtual service.");
  });
});
