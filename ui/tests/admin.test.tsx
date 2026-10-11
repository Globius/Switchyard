import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { quotaLine } from "@/features/admin/Tenants";
import { accessText } from "@/features/admin/Users";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const TENANTS = [
  {
    id: "t1",
    name: "globius-prod",
    version: 3,
    require_approval: true,
    quotas: { virtual_services: 20, service_engines: 4 },
  },
  { id: "t2", name: "globius-lab", version: 1, require_approval: false, quotas: {} },
];
const GRANT = (
  role: "viewer" | "operator" | "tenant-admin" | "system-admin",
  tenant: string | null,
  group: string,
) => ({
  role,
  tenant,
  tenant_id: tenant ? "t" : null,
  via: [{ provider: "Authentik", group }],
});
const USERS = [
  {
    id: "u1",
    provider_id: "pa",
    provider: "Authentik",
    username: "sokoro",
    email: "sokoro@example.com",
    groups: ["sy-prod-ops", "staff"],
    first_login_at: "2026-10-02T10:00:00Z",
    last_login_at: "2026-10-10T17:31:00Z",
    last_seen_at: "2026-10-10T17:40:00Z",
    disabled_at: null,
    grants: [
      GRANT("operator", "globius-prod", "sy-prod-ops"),
      GRANT("viewer", "globius-lab", "sy-lab"),
    ],
  },
  {
    id: "u2",
    provider_id: "pa",
    provider: "Authentik",
    username: "jblake",
    email: null,
    groups: [],
    first_login_at: "2026-08-30T10:00:00Z",
    last_login_at: "2026-08-30T10:00:00Z",
    last_seen_at: "2026-08-30T10:00:00Z",
    disabled_at: "2026-09-01T10:00:00Z",
    grants: [],
  },
];

let isSystem = true;
const calls: Record<string, unknown> = {};
const server = setupServer(
  http.get("*/api/v1/me", () =>
    HttpResponse.json({
      auth: "session",
      break_glass: false,
      csrf_token: "tok",
      email: null,
      id: "s",
      is_system: isSystem,
      name: "Miguel",
      principal: "Miguel",
      session: null,
      tenants: [{ id: "t1", name: "globius-prod", role: "system-admin", via: [] }],
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [],
    }),
  ),
  http.get("*/api/v1/auth/providers", () =>
    HttpResponse.json({
      break_glass: false,
      providers: [{ id: "pa", name: "Authentik", protocol: "oidc" }],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json(TENANTS)),
  http.post("*/api/v1/tenants", async ({ request }) => {
    calls.created = await request.json();
    return HttpResponse.json(TENANTS[1], { status: 201 });
  }),
  http.patch("*/api/v1/tenants/:t", async ({ request, params }) => {
    calls.patched = {
      t: params.t,
      body: await request.json(),
      ifMatch: request.headers.get("if-match"),
    };
    return HttpResponse.json(TENANTS[0]);
  }),
  http.delete("*/api/v1/tenants/globius-lab", () => {
    calls.deleted = true;
    return new HttpResponse(null, { status: 204 });
  }),
  http.get("*/api/v1/users", ({ request }) => {
    calls.userQuery = new URL(request.url).search;
    return HttpResponse.json({ items: USERS, next_cursor: null });
  }),
  http.post("*/api/v1/users/u1/revoke-sessions", () => {
    calls.sessions = true;
    return HttpResponse.json({});
  }),
  http.post("*/api/v1/users/u1/disable", () => {
    calls.disabled = true;
    return HttpResponse.json({});
  }),
  http.post("*/api/v1/users/u2/enable", () => {
    calls.enabled = true;
    return HttpResponse.json({});
  }),
  http.get("*/api/v1/system/tokens", () =>
    HttpResponse.json([
      {
        id: "k1",
        name: "terraform",
        role: "system-admin",
        revoked: false,
        expires_at: "2027-01-01T00:00:00Z",
        created_by: "Miguel",
        created_at: "2026-10-01T00:00:00Z",
        last_used_at: null,
      },
    ]),
  ),
  http.post("*/api/v1/system/tokens", async ({ request }) => {
    calls.token = await request.json();
    return HttpResponse.json(
      { id: "k2", token: "sy_sys_secret", role: "system-admin", expires_at: null },
      { status: 201 },
    );
  }),
  http.delete("*/api/v1/system/tokens/k1", () => {
    calls.revoked = true;
    return new HttpResponse(null, { status: 204 });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  isSystem = true;
  for (const k of Object.keys(calls)) delete calls[k];
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

describe("admin area", () => {
  it("formats quotas and access", () => {
    expect(quotaLine({ virtual_services: 20, service_engines: 4 })).toBe("20 VS · 4 SE");
    expect(quotaLine({})).toBe("—");
    expect(accessText({ grants: [] })).toBe("no grants");
    expect(accessText({ grants: [GRANT("operator", "a", "g"), GRANT("viewer", "b", "g")] })).toBe(
      "2 tenants",
    );
    expect(accessText({ grants: [GRANT("system-admin", null, "g")] })).toBe("system-admin");
  });

  it("redirects /admin to tenants, has its own nav and a way back, and refuses non-admins", async () => {
    const router = renderAt("/admin");
    expect(await screen.findByRole("heading", { name: "Tenants" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/admin/tenants");
    expect(screen.getByText("System admin")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Back to globius-prod" })).toHaveAttribute(
      "href",
      "/t/globius-prod",
    );
    for (const n of ["Users", "Identity providers", "Role mappings", "System tokens", "Audit log"])
      expect(screen.getByRole("link", { name: n })).toBeInTheDocument();
  });

  it("is closed to people who are not system-admin", async () => {
    isSystem = false;
    renderAt("/admin/users");
    expect(await screen.findByText("This area is for system administrators.")).toBeInTheDocument();
  });

  it("lists tenants, saves quotas and approval with If-Match, creates and deletes tenants", async () => {
    renderAt("/admin/tenants");
    expect(await screen.findByText("20 VS · 4 SE")).toBeInTheDocument();
    expect(screen.getByText("Required")).toBeInTheDocument();
    const q1 = await screen.findByLabelText("Limit 1");
    await userEvent.clear(q1);
    await userEvent.type(q1, "25");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls.patched).toEqual({
        t: "globius-prod",
        ifMatch: "3",
        body: { quotas: { virtual_services: 25, service_engines: 4 }, require_approval: true },
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Create tenant" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Name"), "mgmt");
    await userEvent.click(dlg.getByRole("button", { name: "Create tenant" }));
    await waitFor(() =>
      expect(calls.created).toEqual({ name: "mgmt", quotas: {}, require_approval: false }),
    );
    await userEvent.click(screen.getByRole("button", { name: "globius-lab" }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete tenant" }));
    const confirm = within(await screen.findByRole("alertdialog"));
    await userEvent.click(confirm.getByRole("button", { name: "Delete tenant" }));
    await waitFor(() => expect(calls.deleted).toBe(true));
  });

  it("shows a user's groups and effective access and acts on them", async () => {
    renderAt("/admin/users");
    expect(await screen.findByText("2 tenants")).toBeInTheDocument();
    expect(screen.getByText("staff")).toBeInTheDocument();
    expect(screen.getByText("sy-prod-ops", { selector: "td" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "End sessions now" }));
    await userEvent.click(await screen.findByRole("button", { name: "End sessions" }));
    await waitFor(() => expect(calls.sessions).toBe(true));
    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Disable" }));
    await userEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Disable" }),
    );
    await waitFor(() => expect(calls.disabled).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "jblake" }));
    await userEvent.click(await screen.findByRole("button", { name: "Enable" }));
    await waitFor(() => expect(calls.enabled).toBe(true));
    await userEvent.click(screen.getByLabelText("Disabled only"));
    await waitFor(() => expect(String(calls.userQuery)).toContain("disabled=true"));
  });

  it("creates a system token shown once, and revokes one", async () => {
    renderAt("/admin/tokens");
    expect(await screen.findByText("terraform")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create token" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Token name"), "ci");
    await userEvent.click(dlg.getByRole("button", { name: "Create token" }));
    expect(await screen.findByText("sy_sys_secret")).toBeInTheDocument();
    expect(calls.token).toEqual({ name: "ci", ttl_days: 90 });
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await userEvent.click(await screen.findByRole("button", { name: "Revoke token" }));
    await waitFor(() => expect(calls.revoked).toBe(true));
  });
});
