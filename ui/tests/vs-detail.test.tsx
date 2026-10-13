// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

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
  http.get("*/api/v1/tenants/acme/virtual-services/:id/metrics", () =>
    HttpResponse.json({ available: false, reason: "no metrics backend", metrics: null }),
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

  it("shows per-member health from engine status with the failed-check reason", async () => {
    const row = (
      pool_id: string,
      member_id: string,
      status: string,
      engine: string,
      extra = {},
    ) => ({
      pool: "p",
      pool_id,
      member_id,
      address: "10.1.0.1",
      port: 80,
      engine,
      engine_id: engine,
      status,
      check_status: null,
      last_check: null,
      last_change_s: null,
      last_checked_at: "2026-10-11T10:00:00Z",
      ...extra,
    });
    server.use(
      http.get("*/api/v1/tenants/acme/status/virtual-services", () =>
        HttpResponse.json([
          {
            id: "v1",
            name: "web",
            protocol: "http",
            state: "serving",
            health: "degraded",
            members_total: 3,
            members_up: 2,
            members: [
              row("p1", "m1", "UP", "se-a"),
              row("p1", "m1", "DOWN", "se-b", {
                check_status: "L4TOUT",
                last_check: "connection timed out",
                last_change_s: 180,
              }),
              row("p1", "m2", "DRAIN", "se-a"),
            ],
          },
        ]),
      ),
    );
    renderAt("/t/acme/virtual-services/v1");
    const first = (await screen.findByText("10.1.0.1", { selector: "td" })).closest("tr")!;
    expect(await within(first).findByText("Down")).toBeInTheDocument();
    expect(
      within(first).getByText(/L4TOUT · connection timed out · 3 min ago · 2 engines/),
    ).toBeInTheDocument();
    const second = (await screen.findByText("10.1.0.2", { selector: "td" })).closest("tr")!;
    expect(within(second).getAllByText("Draining").length).toBeGreaterThan(0);
  });

  it("links to Grafana only when the server has a Grafana URL", async () => {
    server.use(
      http.get("*/api/v1/me", () =>
        HttpResponse.json({
          auth: "session",
          break_glass: false,
          csrf_token: "tok",
          email: null,
          id: "s",
          is_system: false,
          name: "Miguel",
          principal: "Miguel",
          session: null,
          tenants: [{ id: "t1", name: "acme", role: "tenant-admin", via: [] }],
          links: { grafana: "https://grafana.example.test" },
        }),
      ),
    );
    renderAt("/t/acme/virtual-services/v1");
    expect(await screen.findByRole("link", { name: "Open in Grafana" })).toHaveAttribute(
      "href",
      "https://grafana.example.test",
    );
  });

  it("overview shows the header, tabs, routes and pools", async () => {
    renderAt("/t/acme/virtual-services/v1");
    expect(await screen.findByRole("heading", { name: "web" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Rendered config" })).toBeInTheDocument();
    const routes = await screen.findByText("api.example.test");
    expect(within(routes.closest("tr")!).getByText("api-v2")).toBeInTheDocument();
    expect(screen.getByText("Redirect 301 to https://new.example.test")).toBeInTheDocument();
    expect(screen.getByText("Draining")).toBeInTheDocument();
    expect(await screen.findByText(/Metrics unavailable/)).toBeInTheDocument();
  });

  it("drains a member after confirming, and shows partial failure", async () => {
    stateResult = { state: "drain", applied: ["se-a"], failed: { "se-b": "agent unreachable" } };
    renderAt("/t/acme/virtual-services/v1");
    const row = (await screen.findByText("10.1.0.1:8080")).closest("tr")!;
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
    const row = (await screen.findByText("10.1.0.1:8080")).closest("tr")!;
    await waitFor(() => expect(within(row).getByRole("button", { name: "Drain" })).toBeDisabled());
  });

  it("rendered tab highlights the fragment range and shows the fingerprint", async () => {
    renderAt("/t/acme/virtual-services/v1/rendered");
    expect(await screen.findByText("haproxy/haproxy.cfg")).toBeInTheDocument();
    expect(screen.getByText("aaaaaaaaaaaa")).toBeInTheDocument();
    const lit = document.querySelectorAll("[data-highlight]");
    expect(lit).toHaveLength(2);
  });

  it("rendered tab has an engine picker that shows the files one engine gets", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/virtual-services/v1/engines", () =>
        HttpResponse.json([
          { id: "e1", name: "se-edge-a-01", mgmt_ip: "10.0.0.21", state: "enabled" },
        ]),
      ),
      http.get("*/api/v1/tenants/acme/virtual-services/v1/engines/e1/rendered", () =>
        HttpResponse.json({
          engine: "se-edge-a-01",
          engine_id: "e1",
          se_group_id: "g1",
          fingerprint: "abcdef0123456789",
          files: [
            {
              file: "haproxy/haproxy.cfg",
              scope: "group",
              language: "haproxy",
              content: "global\n",
            },
            {
              file: "keepalived.conf",
              scope: "engine",
              language: "plaintext",
              content: "vrrp_instance X {}\n",
            },
          ],
        }),
      ),
    );
    renderAt("/t/acme/virtual-services/v1/rendered");
    const pick = await screen.findByLabelText("Engine");
    await userEvent.selectOptions(
      pick,
      await screen.findByRole("option", { name: /se-edge-a-01/ }),
    );
    expect(await screen.findByText(/keepalived\.conf · this engine/)).toBeInTheDocument();
    expect(screen.getByText(/haproxy\/haproxy\.cfg · SE group/)).toBeInTheDocument();
    expect(screen.getByText(/What se-edge-a-01 runs for this service/)).toBeInTheDocument();
  });

  it("rendered tab explains when nothing is rendered", async () => {
    empty = true;
    renderAt("/t/acme/virtual-services/v1/rendered");
    expect(await screen.findByText("Nothing is rendered")).toBeInTheDocument();
  });

  it("history is one timeline: revisions with their field diff, runtime actions and approvals, with filters", async () => {
    server.use(
      http.get("*/api/v1/audit-actions", () =>
        HttpResponse.json([
          { action: "member.state", category: "pool_member", description: "d" },
          { action: "change_set.reject", category: "change_set", description: "d" },
          { action: "tenant.update", category: "tenant", description: "d" },
        ]),
      ),
      http.get("*/api/v1/tenants/acme/audit-events", () =>
        HttpResponse.json({
          items: [
            {
              action: "member.state",
              actor: "miguel",
              at: "2026-10-10T11:00:00Z",
              object: "pool api",
              diff: null,
              request_id: null,
              tenant_id: "t1",
            },
            {
              action: "change_set.reject",
              actor: "miguel",
              at: "2026-10-09T11:00:00Z",
              object: "change_set:x",
              diff: null,
              request_id: null,
              tenant_id: "t1",
            },
            {
              action: "tenant.update",
              actor: "miguel",
              at: "2026-10-08T11:00:00Z",
              object: "t",
              diff: null,
              request_id: null,
              tenant_id: "t1",
            },
          ],
          next_cursor: null,
        }),
      ),
      http.get("*/api/v1/tenants/acme/revisions/42", () =>
        HttpResponse.json({
          number: 42,
          summary: "Raise maxconn",
          author: "dana",
          created_at: "2026-10-10T10:00:00Z",
          git_sha: null,
          snapshot: { virtual_services: [{ id: "v1", name: "web", maxconn: 200 }] },
        }),
      ),
      http.get("*/api/v1/tenants/acme/revisions/41", () =>
        HttpResponse.json({
          number: 41,
          summary: "old",
          author: "dana",
          created_at: "2026-10-09T10:00:00Z",
          git_sha: null,
          snapshot: { virtual_services: [{ id: "v1", name: "web", maxconn: 100 }] },
        }),
      ),
    );
    renderAt("/t/acme/virtual-services/v1/history");
    expect(await screen.findByText("Raise maxconn")).toBeInTheDocument();
    expect(screen.getByText("member.state")).toBeInTheDocument();
    expect(screen.getByText("change_set.reject")).toBeInTheDocument();
    expect(screen.queryByText("tenant.update")).not.toBeInTheDocument();
    expect(
      screen.getByText("runtime action, not a revision", { exact: false }),
    ).toBeInTheDocument();
    expect(await screen.findByText("maxconn: 100")).toBeInTheDocument();
    expect(screen.getByText("maxconn: 200")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: "Runtime actions" }));
    expect(screen.queryByText("Raise maxconn")).not.toBeInTheDocument();
    expect(screen.getByText("member.state")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("radio", { name: "Approvals" }));
    expect(screen.queryByText("member.state")).not.toBeInTheDocument();
    expect(screen.getByText("change_set.reject")).toBeInTheDocument();
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
    expect(screen.getByText("Deleted in draft")).toBeInTheDocument();
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

  it("lists the field findings of a rejected draft on the rendered tab", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/virtual-services/v1/rendered", () =>
        HttpResponse.json(
          {
            title: "Validation failed",
            status: 422,
            detail: "the state breaks domain rules",
            errors: [
              {
                path: "virtual_services.web.vip",
                code: "vip_outside_network",
                message: "10.9.9.9 is outside 10.0.0.0/24",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    renderAt("/t/acme/virtual-services/v1/rendered");
    expect(await screen.findByText(/10.9.9.9 is outside 10.0.0.0\/24/)).toBeInTheDocument();
    expect(screen.getByText("virtual_services.web.vip")).toBeInTheDocument();
  });
});
