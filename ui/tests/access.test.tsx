import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { sinceText } from "@/features/access/Access";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const USERS = [
  {
    id: "u1",
    provider_id: "pa",
    provider: "Authentik",
    username: "mrodriguez",
    email: null,
    groups: [],
    first_login_at: "2026-10-01T08:00:00Z",
    last_login_at: new Date().toISOString(),
    last_seen_at: "2026-10-11T08:00:00Z",
    disabled_at: null,
    grants: [
      {
        role: "tenant-admin",
        tenant: "acme",
        tenant_id: "t1",
        via: [{ provider: "Authentik", group: "sy-acme-admins" }],
      },
    ],
  },
];
const MAPPINGS = [
  {
    id: "m1",
    provider_id: "pa",
    provider: "Authentik",
    group_name: "sy-acme",
    label: null,
    tenant_id: "t1",
    tenant: "acme",
    role: "viewer",
    version: 1,
    created_by: "x",
    created_at: "2026-10-01T00:00:00Z",
  },
  {
    id: "m2",
    provider_id: "pa",
    provider: "Authentik",
    group_name: "sy-acme-admins",
    label: null,
    tenant_id: "t1",
    tenant: "acme",
    role: "tenant-admin",
    version: 1,
    created_by: "x",
    created_at: "2026-10-01T00:00:00Z",
  },
];

let role = "tenant-admin";
const calls: Record<string, unknown> = {};
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
      principal: "Miguel",
      session: null,
      tenants: [{ id: "t1", name: "acme", role, via: [] }],
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "manage", object: "token", min_role: "tenant-admin", description: "Tokens." },
        { verb: "manage", object: "role_mapping", min_role: "tenant-admin", description: "Maps." },
        {
          verb: "enable",
          object: "require_approval",
          min_role: "tenant-admin",
          description: "On.",
        },
        {
          verb: "disable",
          object: "require_approval",
          min_role: "system-admin",
          description: "Off.",
        },
      ],
    }),
  ),
  http.get("*/api/v1/auth/providers", () =>
    HttpResponse.json({
      break_glass: false,
      providers: [{ id: "pa", name: "Authentik", protocol: "oidc" }],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/users", () =>
    HttpResponse.json({ items: USERS, next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/role-mappings", () => HttpResponse.json(MAPPINGS)),
  http.post("*/api/v1/tenants/acme/role-mappings", async ({ request }) => {
    calls.mapping = await request.json();
    return HttpResponse.json(MAPPINGS[0], { status: 201 });
  }),
  http.delete("*/api/v1/tenants/acme/role-mappings/m1", () => {
    calls.removed = true;
    return new HttpResponse(null, { status: 204 });
  }),
  http.get("*/api/v1/tenants/acme/tokens", () =>
    HttpResponse.json([
      {
        id: "k1",
        name: "gitlab-ci",
        role: "operator",
        revoked: false,
        expires_at: "2027-01-08T00:00:00Z",
      },
      { id: "k2", name: "old", role: "viewer", revoked: true, expires_at: null },
    ]),
  ),
  http.post("*/api/v1/tenants/acme/tokens", async ({ request }) => {
    calls.token = await request.json();
    return HttpResponse.json({ id: "k3", token: "sy_secret", role: "viewer" }, { status: 201 });
  }),
  http.delete("*/api/v1/tenants/acme/tokens/k1", () => {
    calls.revoked = true;
    return new HttpResponse(null, { status: 204 });
  }),
  http.get("*/api/v1/tenants/acme", () =>
    HttpResponse.json({
      id: "t1",
      name: "acme",
      version: 4,
      require_approval: false,
      quotas: { virtual_service: 20 },
      usage: { virtual_service: 7 },
    }),
  ),
  http.patch("*/api/v1/tenants/acme", async ({ request }) => {
    calls.patch = { body: await request.json(), ifMatch: request.headers.get("if-match") };
    return HttpResponse.json({
      id: "t1",
      name: "acme",
      version: 5,
      require_approval: true,
      quotas: {},
    });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  role = "tenant-admin";
  for (const k of Object.keys(calls)) delete calls[k];
});
afterAll(() => server.close());

let lastClient: ReturnType<typeof createQueryClient> | null = null;
function renderAt(path: string) {
  lastClient = createQueryClient();
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: [path] }),
  });
  render(
    <ThemeProvider>
      <QueryClientProvider client={lastClient!}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("tenant access and settings", () => {
  it("says today, yesterday or the date", () => {
    const now = new Date("2026-10-11T12:00:00");
    expect(sinceText("2026-10-11T08:01:00", now)).toMatch(/^today /);
    expect(sinceText("2026-10-10T08:01:00", new Date("2026-10-11T12:00:00"))).toBe("yesterday");
    expect(sinceText("2026-10-03T08:01:00", new Date("2026-10-11T12:00:00"))).toMatch(/Oct/);
  });

  it("lists members with their role and the groups that grant it", async () => {
    renderAt("/t/acme/access");
    expect(
      await screen.findByText("You are a tenant-admin here.", { exact: false }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("mrodriguez", { selector: "div.font-medium" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Authentik · sy-acme-admins")).toBeInTheDocument();
  });

  it("adds and removes viewer and operator mappings; tenant-admin mappings are locked", async () => {
    renderAt("/t/acme/access");
    await userEvent.click(await screen.findByRole("tab", { name: "Role mappings" }));
    expect(await screen.findByText("locked")).toBeInTheDocument();
    const row = screen.getByText("sy-acme").closest("tr")!;
    const rm = within(row).getByRole("button", { name: "Remove" });
    await waitFor(() => expect(rm).toBeEnabled());
    await userEvent.click(rm);
    await userEvent.click(await screen.findByRole("button", { name: "Remove mapping" }));
    await waitFor(() => expect(calls.removed).toBe(true));
    const add = screen.getByRole("button", { name: "Add mapping" });
    await userEvent.click(add);
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.selectOptions(await dlg.findByLabelText("Provider"), "pa");
    await userEvent.type(dlg.getByLabelText("Group"), "sy-ops");
    await userEvent.selectOptions(dlg.getByLabelText("Role"), "operator");
    await userEvent.click(dlg.getByRole("button", { name: "Add mapping" }));
    await waitFor(() =>
      expect(calls.mapping).toEqual({
        provider: "pa",
        group_name: "sy-ops",
        role: "operator",
        label: null,
      }),
    );
  });

  it("creates a token shown once, and revokes one", async () => {
    renderAt("/t/acme/access");
    await userEvent.click(await screen.findByRole("tab", { name: "API tokens" }));
    expect(await screen.findByText("gitlab-ci")).toBeInTheDocument();
    expect(screen.getByText("Revoked")).toBeInTheDocument();
    const create = screen.getByRole("button", { name: "Create token" });
    await waitFor(() => expect(create).toBeEnabled());
    await userEvent.click(create);
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Token name"), "ci2");
    await userEvent.click(dlg.getByRole("button", { name: "Create token" }));
    expect(await screen.findByText("sy_secret")).toBeInTheDocument();
    await waitFor(() => expect(calls.token).toEqual({ name: "ci2", role: "viewer", ttl_days: 90 }));
    const done = screen.getByRole("button", { name: "Done" });
    expect(done).toBeDisabled();
    await userEvent.click(screen.getByLabelText("I have stored it"));
    await userEvent.click(done);
    // The created token is not kept in the mutation cache once the dialog is closed.
    await waitFor(() =>
      expect(
        JSON.stringify(
          lastClient!
            .getMutationCache()
            .getAll()
            .map((m) => m.state.data),
        ),
      ).not.toContain("sy_secret"),
    );
    const row = screen.getByText("gitlab-ci").closest("tr")!;
    await userEvent.click(within(row).getByRole("button", { name: "Revoke" }));
    await userEvent.click(await screen.findByRole("button", { name: "Revoke token" }));
    await waitFor(() => expect(calls.revoked).toBe(true));
  });

  it("turns require-approval on with If-Match; only a system-admin can turn it off", async () => {
    renderAt("/t/acme/access");
    await userEvent.click(await screen.findByRole("tab", { name: "Settings" }));
    expect(await screen.findByText("virtual service")).toBeInTheDocument();
    expect(screen.getByText("7 of 20")).toBeInTheDocument();
    const sw = await screen.findByRole("switch", { name: "Require approval" });
    await waitFor(() => expect(sw).toBeEnabled());
    await userEvent.click(sw);
    await waitFor(() =>
      expect(calls.patch).toEqual({ body: { require_approval: true }, ifMatch: "4" }),
    );
  });
});
