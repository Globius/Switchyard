import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { fieldOf } from "@/components/sy/Findings";
import { findingsFor, findingsOfError } from "@/features/vs/findings";
import { ApiError } from "@/api/client";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const VIP_PROBLEMS = [
  {
    path: "virtual_services.web.vip",
    code: "vip_outside_network",
    message: "10.9.9.9 is outside 192.0.2.0/24",
  },
  {
    path: "virtual_services.web.ports.0.port",
    code: "collision",
    message: "Port 443 is already used.",
  },
  { path: "virtual_services.other.vip", code: "x", message: "belongs to another service" },
];

describe("finding helpers", () => {
  it("maps API paths to the form field to open", () => {
    expect(fieldOf("virtual_services.web.vip")).toBe("vip");
    expect(fieldOf("virtual_services.web.ports.0.port")).toBe("ports.0");
    expect(fieldOf("routes[2].match.host")).toBe("routes.2");
    expect(fieldOf("certificate_ids.1")).toBe("certificate_ids.1");
    expect(fieldOf("name")).toBe("name");
  });
  it("keeps only one service's problems, paths unchanged", () => {
    expect(findingsFor(VIP_PROBLEMS, "web").map((f) => f.path)).toEqual([
      "virtual_services.web.vip",
      "virtual_services.web.ports.0.port",
    ]);
    expect(findingsFor(VIP_PROBLEMS, undefined)).toEqual([]);
  });
  it("reads findings from a 422 and ignores other errors", () => {
    const e = new ApiError(422, {
      title: "x",
      status: 422,
      detail: "x",
      errors: [{ path: "a.b", code: "c", message: "m" }],
    });
    expect(findingsOfError(e)).toEqual([{ path: "a.b", code: "c", message: "m" }]);
    expect(findingsOfError(new ApiError(500))).toEqual([]);
    expect(findingsOfError(new Error("x"))).toEqual([]);
  });
});

let problems: object[] = VIP_PROBLEMS;
let renderedStatus = 422;
const VS = {
  id: "v1",
  name: "web",
  protocol: "http",
  vip: "10.9.9.9",
  ports: [
    { port: 443, tls: false, http2: false, proxy_protocol: false, https_redirect_port: null },
  ],
  certificate_ids: [],
  enabled: true,
  routes: [],
  policy_set_ids: [],
  enforce_allow_list: false,
  default_pool_id: null,
  se_group_id: "g1",
  version: 1,
  draft_status: "modified",
  changed_fields: ["vip"],
  tenant_id: "t1",
};
const seen: string[] = [];
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
      tenants: [{ id: "t1", name: "acme", role: "operator", via: [] }],
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "write", object: "application", min_role: "operator", description: "w" },
        { verb: "edit", object: "draft", min_role: "operator", description: "e" },
        { verb: "apply", object: "change_set", min_role: "operator", description: "a" },
        { verb: "request_approval", object: "change_set", min_role: "operator", description: "r" },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () =>
    HttpResponse.json([
      { id: "t1", name: "acme", quotas: {}, require_approval: false, version: 1 },
    ]),
  ),
  http.get("*/api/v1/tenants/acme/change-sets", ({ request }) =>
    HttpResponse.json({
      items:
        new URL(request.url).searchParams.get("state") === "draft"
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
  http.get("*/api/v1/tenants/acme/change-sets/cs1", () =>
    HttpResponse.json({
      id: "cs1",
      state: "draft",
      author: "me",
      summary: "d",
      base_revision: 1,
      created_at: "2026-10-10T10:00:00Z",
      approver: null,
      reject_comment: null,
      error: null,
      ops: [],
    }),
  ),
  http.get("*/api/v1/tenants/acme/change-sets/cs1/diff", () =>
    HttpResponse.json({
      objects: [
        {
          type: "virtual_service",
          id: "v1",
          name: "web",
          status: "modified",
          changed_fields: ["vip"],
        },
      ],
      problems,
      warnings: [],
      impact: { vips: [], services: [], se_groups: [], hostnames: [] },
      se_groups: [],
    }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services/v1", ({ request }) => {
    seen.push(request.url);
    return HttpResponse.json(VS);
  }),
  http.get("*/api/v1/tenants/acme/virtual-services/v1/rendered", ({ request }) => {
    seen.push(request.url);
    if (new URL(request.url).searchParams.has("change_set") && renderedStatus === 422)
      return HttpResponse.json(
        {
          title: "Validation failed",
          status: 422,
          detail: "the state breaks domain rules",
          errors: [
            {
              path: "virtual_services.web.vip",
              code: "x",
              message: "10.9.9.9 is outside 192.0.2.0/24",
            },
          ],
        },
        { status: 422 },
      );
    return HttpResponse.json({
      items: [
        {
          file: "haproxy.cfg",
          language: "haproxy",
          fingerprint: "a".repeat(64),
          se_group_id: "g1",
          content: "frontend web\n",
          range: [1, 1],
        },
      ],
    });
  }),
  http.get("*/api/v1/tenants/acme/pools", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/status/virtual-services", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/metrics/virtual-services", () =>
    HttpResponse.json({ available: false, reason: "none", window_s: 900, items: [] }),
  ),
  http.get("*/api/v1/tenants/acme/se-groups", () =>
    HttpResponse.json({ items: [{ id: "g1", name: "edge" }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/certificates", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/policy-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/schema/virtual-service", () =>
    HttpResponse.json({
      protocol: "http",
      algorithms: [],
      member_port_required: false,
      monitor_types: [],
      pool_forbidden: [],
      port_forbidden: [],
      service_forbidden: [],
    }),
  ),
  http.get("*/api/v1/tenants/acme/jobs", () => HttpResponse.json({ items: [], next_cursor: null })),
  http.get("*/api/v1/tenants/acme/pools/:id", () => HttpResponse.json({ id: "x" })),
  http.get("*/api/v1/tenants/acme/health-monitors", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  problems = VIP_PROBLEMS;
  renderedStatus = 422;
  seen.length = 0;
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

describe("invalid draft on the service tabs", () => {
  it("Overview shows the invalid-draft card with Go to field links and can show the live version", async () => {
    renderAt("/t/acme/virtual-services/v1");
    expect(await screen.findByText("Your draft is invalid")).toBeInTheDocument();
    expect(screen.getByText("10.9.9.9 is outside 192.0.2.0/24")).toBeInTheDocument();
    expect(screen.queryByText("belongs to another service")).not.toBeInTheDocument();
    const links = screen.getAllByRole("link", { name: "Go to field" });
    expect(links[0]).toHaveAttribute("href", "/t/acme/virtual-services/v1/settings?field=vip");
    expect(links[1]).toHaveAttribute("href", "/t/acme/virtual-services/v1/settings?field=ports.0");
    await userEvent.click(screen.getByRole("button", { name: "Show the live version" }));
    expect(await screen.findByRole("heading", { name: "Routes" })).toBeInTheDocument();
    expect(screen.queryByText("Your draft is invalid")).not.toBeInTheDocument();
  });

  it("Rendered config shows the same card from the 422 and the live version on request", async () => {
    problems = [];
    renderAt("/t/acme/virtual-services/v1/rendered");
    expect(await screen.findByText("Your draft is invalid")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show the live version" }));
    expect(await screen.findByText("haproxy.cfg")).toBeInTheDocument();
    expect(screen.getByText(/Showing the live version/)).toBeInTheDocument();
    expect(seen.at(-1)).not.toContain("change_set");
  });

  it("Go to field opens Settings with that field focused", async () => {
    renderAt("/t/acme/virtual-services/v1/settings?field=vip");
    const vip = await screen.findByLabelText("VIP");
    await waitFor(() => expect(vip).toHaveFocus());
  });
});

describe("invalid draft in Review", () => {
  it("lists the findings, disables Apply and says the config can't be rendered", async () => {
    renderAt("/t/acme/review/cs1");
    const alert = await screen.findByRole("alert", { name: "Your draft is invalid" });
    expect(within(alert).getByText("10.9.9.9 is outside 192.0.2.0/24")).toBeInTheDocument();
    const panel = within(await screen.findByRole("complementary", { name: "Apply" }));
    expect(panel.getByRole("button", { name: "Apply" })).toBeDisabled();
    await userEvent.click(screen.getByRole("tab", { name: "Config" }));
    expect(
      await screen.findByText(/can't be rendered while the draft is invalid/),
    ).toBeInTheDocument();
  });
});

describe("forms with several findings", () => {
  it("shows a summary that links each placeable finding and keeps others as text", async () => {
    server.use(
      http.patch("*/api/v1/tenants/acme/virtual-services/v1", () =>
        HttpResponse.json(
          {
            title: "Invalid",
            status: 422,
            detail: "x",
            errors: [
              { path: "vip", code: "x", message: "VIP is not in the network." },
              { path: "ports.0.port", code: "x", message: "Port is taken." },
              { path: "something_else.deep", code: "x", message: "Cannot place this one." },
            ],
          },
          { status: 422 },
        ),
      ),
      http.post("*/api/v1/tenants/acme/change-sets/:cs/ops", () =>
        HttpResponse.json({}, { status: 500 }),
      ),
    );
    renderAt("/t/acme/virtual-services/v1/settings");
    const name = await screen.findByLabelText("Name");
    await waitFor(() => expect(name).toBeEnabled());
    await userEvent.type(name, "x");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    const summary = await screen.findByText("3 things need fixing");
    const box = within(summary.closest("[role=alert]") as HTMLElement);
    expect(box.getByRole("button", { name: "VIP is not in the network." })).toBeInTheDocument();
    expect(box.getByRole("button", { name: "Port is taken." })).toBeInTheDocument();
    expect(box.queryByRole("button", { name: "Cannot place this one." })).not.toBeInTheDocument();
    expect(box.getByText("Cannot place this one.")).toBeInTheDocument();
    await userEvent.click(box.getByRole("button", { name: "VIP is not in the network." }));
    expect(screen.getByLabelText("VIP")).toHaveFocus();
  });
});
