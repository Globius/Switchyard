import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import {
  resourceLabel,
  suggestedLimit,
  usagePercent,
  usageTone,
} from "@/features/portal/resources";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

describe("quota helpers", () => {
  it("colours a quota by how full it is, never by colour alone", () => {
    expect(usageTone(3, 10)).toBe("healthy");
    expect(usageTone(8, 10)).toBe("degraded");
    expect(usageTone(10, 10)).toBe("down");
    expect(usageTone(0, 0)).toBe("down");
    expect(usagePercent(5, 10)).toBe(50);
    expect(usagePercent(15, 10)).toBe(100);
    expect(usagePercent(1, 0)).toBe(100);
  });
  it("names keys and suggests a higher limit", () => {
    expect(resourceLabel("virtual_service")).toBe("Virtual services");
    expect(resourceLabel("something_new")).toBe("something new");
    expect(suggestedLimit(20)).toBe(30);
    expect(suggestedLimit(1)).toBe(2);
    expect(suggestedLimit(0)).toBe(1);
  });
});

let role = "tenant-admin";
let isSystem = false;
const me = () => ({
  auth: "session",
  break_glass: false,
  csrf_token: "tok",
  email: "m@example.test",
  id: "s1",
  is_system: isSystem,
  name: "Miguel",
  principal: "Miguel",
  session: null,
  tenants: [{ id: "t1", name: "acme", role, via: [] }],
});
const request = (o: object) => ({
  id: "r0",
  tenant_id: "t1",
  tenant: "acme",
  resource: "pool",
  current_limit: 10,
  requested_limit: 20,
  reason: "growth",
  state: "pending",
  requested_by: "Miguel",
  requested_at: "2026-10-12T09:00:00Z",
  granted_limit: null,
  decided_by: null,
  decided_at: null,
  decision_note: null,
  ...o,
});

let mine: object[] = [];
let queue: object[] = [];
const posted: { url: string; body: unknown }[] = [];
const server = setupServer(
  http.get("*/api/v1/me", () => HttpResponse.json(me())),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "request", object: "quota_request", min_role: "tenant-admin", description: "Ask." },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme", () =>
    HttpResponse.json({
      id: "t1",
      name: "acme",
      version: 1,
      members: 2,
      require_approval: false,
      quotas: { pool: 10, virtual_service: 20, certificate: 5 },
      usage: { pool: 10, virtual_service: 17, certificate: 1 },
    }),
  ),
  http.get("*/api/v1/tenants/acme/quota-requests", () => HttpResponse.json(mine)),
  http.post("*/api/v1/tenants/acme/quota-requests", async ({ request: r }) => {
    posted.push({ url: r.url, body: await r.json() });
    return HttpResponse.json(request({}), { status: 201 });
  }),
  http.post("*/api/v1/tenants/acme/quota-requests/:id/withdraw", ({ params }) => {
    posted.push({ url: `withdraw ${String(params.id)}`, body: null });
    return HttpResponse.json(request({ state: "withdrawn" }));
  }),
  http.get("*/api/v1/quota-requests", ({ request: r }) => {
    const state = new URL(r.url).searchParams.get("state");
    return HttpResponse.json(
      queue.filter((q) => (q as { state: string }).state === (state ?? "pending")),
    );
  }),
  http.post("*/api/v1/quota-requests/:id/:verb", async ({ request: r, params }) => {
    posted.push({ url: `${String(params.verb)} ${String(params.id)}`, body: await r.json() });
    return HttpResponse.json(request({ state: "approved" }));
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  role = "tenant-admin";
  isSystem = false;
  mine = [];
  queue = [];
  posted.length = 0;
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

describe("tenant portal", () => {
  it("shows usage against each limit with a word for how full it is", async () => {
    renderAt("/t/acme/portal");
    expect(await screen.findByRole("meter", { name: "10 of 10 used" })).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "17 of 20 used" })).toBeInTheDocument();
    expect(screen.getByText("Full")).toBeInTheDocument();
    expect(screen.getByText("Nearly full")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Publish an application/ })).toBeInTheDocument();
  });

  it("sends a request for more with the reason", async () => {
    renderAt("/t/acme/portal");
    await userEvent.click(await screen.findByRole("button", { name: "Ask for more pools" }));
    expect(screen.getByLabelText("New limit")).toHaveValue(15);
    const send = screen.getByRole("button", { name: "Send request" });
    expect(send).toBeDisabled();
    await userEvent.clear(screen.getByLabelText("New limit"));
    await userEvent.type(screen.getByLabelText("New limit"), "25");
    await userEvent.type(screen.getByLabelText("Why do you need it?"), "Two teams join");
    await userEvent.click(send);
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]?.body).toEqual({
      resource: "pool",
      requested_limit: 25,
      reason: "Two teams join",
    });
  });

  it("shows the API's reason when a request is refused", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/quota-requests", () =>
        HttpResponse.json(
          {
            title: "Invalid",
            status: 422,
            errors: [
              {
                path: "requested_limit",
                code: "invalid",
                message: "the requested limit must be higher than the current limit (10)",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    renderAt("/t/acme/portal");
    await userEvent.click(await screen.findByRole("button", { name: "Ask for more pools" }));
    await userEvent.type(screen.getByLabelText("Why do you need it?"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Send request" }));
    expect(await screen.findByText(/must be higher than the current limit/)).toBeInTheDocument();
  });

  it("replaces the button with a pill while a request waits, and can withdraw it", async () => {
    mine = [request({ id: "r9" })];
    renderAt("/t/acme/portal");
    expect(await screen.findByText("Request pending")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Ask for more pools" })).not.toBeInTheDocument();
    expect(screen.getByText("Waiting for a decision")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Withdraw" }));
    await waitFor(() => expect(posted[0]?.url).toBe("withdraw r9"));
  });

  it("shows the answer and the granted limit on a decided request", async () => {
    mine = [
      request({
        state: "approved",
        granted_limit: 15,
        decision_note: "15 for now",
        decided_at: "2026-10-12T10:00:00Z",
      }),
      request({ id: "r2", resource: "certificate", state: "denied", decision_note: "No" }),
    ];
    renderAt("/t/acme/portal");
    expect(await screen.findByText("10 → 15")).toBeInTheDocument();
    expect(screen.getByText("15 for now")).toBeInTheDocument();
    expect(screen.getByText("Denied")).toBeInTheDocument();
  });

  it("lets an operator read but not ask", async () => {
    role = "operator";
    renderAt("/t/acme/portal");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Ask for more pools" })).toBeDisabled(),
    );
  });
});

describe("admin queue", () => {
  it("lists waiting requests and approves with a smaller grant", async () => {
    isSystem = true;
    queue = [request({ id: "q1" }), request({ id: "q2", state: "approved" })];
    renderAt("/admin/quota-requests");
    const row = (await screen.findByText("growth")).closest("tr")!;
    expect(within(row).getByText("10 → 20")).toBeInTheDocument();
    expect(screen.queryByText("Waiting for a decision", { selector: "span" })).toBeTruthy();
    await userEvent.click(within(row).getByRole("button", { name: /Approve acme Pools/ }));
    const grant = screen.getByLabelText("Limit to grant");
    expect(grant).toHaveValue(20);
    await userEvent.clear(grant);
    await userEvent.type(grant, "15");
    await userEvent.type(screen.getByLabelText("Note for the tenant (optional)"), "15 for now");
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toEqual({
      url: "approve q1",
      body: { granted_limit: 15, note: "15 for now" },
    });
  });

  it("will not deny without a reason", async () => {
    isSystem = true;
    queue = [request({ id: "q1" })];
    renderAt("/admin/quota-requests");
    await userEvent.click(await screen.findByRole("button", { name: /Deny acme Pools/ }));
    const deny = screen.getAllByRole("button", { name: "Deny" }).at(-1)!;
    expect(deny).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Why is it denied?"), "Delete unused pools");
    expect(deny).toBeEnabled();
    await userEvent.click(deny);
    await waitFor(() =>
      expect(posted[0]).toEqual({ url: "deny q1", body: { note: "Delete unused pools" } }),
    );
  });

  it("counts the waiting requests in the nav and switches state", async () => {
    isSystem = true;
    queue = [request({ id: "q1" }), request({ id: "q3", resource: "certificate" })];
    renderAt("/admin/quota-requests");
    expect(await screen.findByLabelText("2 waiting")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Denied" }));
    expect(await screen.findByText("No requests in this state")).toBeInTheDocument();
  });

  it("says when nothing is waiting", async () => {
    isSystem = true;
    renderAt("/admin/quota-requests");
    expect(await screen.findByText("Nothing is waiting")).toBeInTheDocument();
  });
});
