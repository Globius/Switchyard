import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { quotaLine } from "@/features/admin/Tenants";
import { exportUrl } from "@/features/admin/AuditLog";
import { mappingSummary } from "@/features/admin/IdentityProviders";
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
    usage: { virtual_services: 7, service_engines: 4 },
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
    expect(await screen.findByText("Used")).toBeInTheDocument();
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
    await userEvent.click(screen.getByLabelText("I have stored it"));
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await userEvent.click(await screen.findByRole("button", { name: "Revoke token" }));
    await waitFor(() => expect(calls.revoked).toBe(true));
  });

  it("edits role mappings: system-wide and per tenant, with the permission matrix", async () => {
    const mappings = [
      {
        id: "m1",
        provider_id: "pa",
        provider: "Authentik",
        group_name: "sy-platform-admins",
        label: null,
        tenant_id: null,
        tenant: null,
        role: "system-admin",
        version: 2,
        created_by: "Miguel",
        created_at: "2026-10-02T00:00:00Z",
      },
      {
        id: "m2",
        provider_id: "pa",
        provider: "Authentik",
        group_name: "sy-prod",
        label: "Prod",
        tenant_id: "t1",
        tenant: "globius-prod",
        role: "viewer",
        version: 1,
        created_by: "Miguel",
        created_at: "2026-10-02T00:00:00Z",
      },
    ];
    server.use(
      http.get("*/api/v1/role-mappings", () => HttpResponse.json(mappings)),
      http.get("*/api/v1/permissions", () =>
        HttpResponse.json({
          roles: ["viewer", "operator", "tenant-admin", "system-admin"],
          permissions: [
            { verb: "read", object: "x", min_role: "viewer", description: "Read everything" },
            {
              verb: "manage",
              object: "tenant",
              min_role: "system-admin",
              description: "Create tenants",
            },
          ],
        }),
      ),
      http.patch("*/api/v1/role-mappings/m2", async ({ request }) => {
        calls.rolePatch = { body: await request.json(), ifMatch: request.headers.get("if-match") };
        return HttpResponse.json(mappings[1]);
      }),
      http.post("*/api/v1/role-mappings", async ({ request }) => {
        calls.mapAdd = await request.json();
        return HttpResponse.json(mappings[1], { status: 201 });
      }),
      http.delete("*/api/v1/role-mappings/m1", () => {
        calls.mapDel = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderAt("/admin/role-mappings");
    expect(await screen.findByText("sy-platform-admins")).toBeInTheDocument();
    expect(await screen.findByText("Read everything")).toBeInTheDocument();
    expect(screen.getAllByLabelText("allowed").length).toBeGreaterThan(4);
    await userEvent.selectOptions(await screen.findByLabelText("Role of sy-prod"), "operator");
    await waitFor(() =>
      expect(calls.rolePatch).toEqual({ body: { role: "operator" }, ifMatch: "1" }),
    );
    const tenantArea = screen.getByText("Per tenant").closest("section")!;
    const add = within(tenantArea);
    await userEvent.selectOptions(add.getByLabelText("Provider"), "pa");
    await userEvent.type(add.getByLabelText("Group name or object ID"), "sy-new");
    await userEvent.click(add.getByRole("button", { name: "Add" }));
    await waitFor(() =>
      expect(calls.mapAdd).toEqual({
        provider: "pa",
        group_name: "sy-new",
        role: "viewer",
        label: null,
        tenant_id: "t1",
      }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Remove mapping for sy-platform-admins" }),
    );
    await waitFor(() => expect(calls.mapDel).toBe(true));
  });

  it("audit log: filters, expandable diff, break-glass marker and CSV", async () => {
    const queries: string[] = [];
    server.use(
      http.get("*/api/v1/audit-actions", () =>
        HttpResponse.json([
          { action: "tenant.update", category: "write", description: "d" },
          { action: "member.drain", category: "runtime", description: "d" },
        ]),
      ),
      http.get("*/api/v1/audit-events", ({ request }) => {
        queries.push(new URL(request.url).search);
        return HttpResponse.json({
          items: [
            {
              at: "2026-10-06T14:02:00Z",
              actor: "Miguel",
              action: "tenant.update",
              object: "globius-prod",
              tenant_id: "t1",
              request_id: "req_3b6f",
              diff: { require_approval: true },
            },
            {
              at: "2026-10-06T14:03:00Z",
              actor: "Miguel",
              action: "member.drain",
              object: "pool api-v2",
              tenant_id: "t1",
              request_id: null,
              diff: null,
            },
          ],
          next_cursor: null,
        });
      }),
    );
    renderAt("/admin/audit-log");
    expect(await screen.findByText("tenant.update", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Show details" })[0]!);
    expect(await screen.findByText(/"require_approval": true/)).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Show details" })[0]!);
    expect(await screen.findByText("No details recorded.")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Action"), "member.drain");
    await waitFor(() => expect(queries.some((q) => q.includes("action=member.drain"))).toBe(true));
    await userEvent.selectOptions(screen.getByLabelText("Time range"), "all");
    await waitFor(() => expect(queries.at(-1)).not.toContain("since="));
    expect(
      exportUrl(
        "csv",
        { q: "x", tenant: "acme", sinceMs: 3_600_000 },
        Date.parse("2026-10-11T12:00:00Z"),
      ),
    ).toBe(
      "/api/v1/audit-events/export?format=csv&limit=500&q=x&tenant=acme&since=2026-10-11T11%3A00%3A00.000Z",
    );
    expect(screen.getByRole("link", { name: "Export CSV" })).toHaveAttribute(
      "href",
      expect.stringContaining("format=csv"),
    );
    expect(screen.getByRole("link", { name: "Export JSON Lines" })).toHaveAttribute(
      "href",
      expect.stringContaining("format=jsonl"),
    );
  });

  it("summarises mappings per provider", () => {
    const rows = [
      { provider_id: "a", tenant_id: "t1", tenant: "prod" },
      { provider_id: "a", tenant_id: "t2", tenant: "lab" },
      { provider_id: "b", tenant_id: "t1", tenant: "prod" },
    ];
    expect(mappingSummary(rows, "a")).toBe("2 across 2 tenants");
    expect(mappingSummary(rows, "b")).toBe("1 in prod");
    expect(mappingSummary(rows, "c")).toBe("none");
  });

  it("lists providers, toggles one, edits an OIDC provider, and runs break-glass on and off", async () => {
    const IDP = [
      {
        id: "i1",
        protocol: "oidc",
        name: "Authentik",
        enabled: true,
        version: 3,
        created_at: "2026-10-01T00:00:00Z",
        warnings: ["groups claim missing"],
        issuer_url: "https://auth.example.com/",
        client_id: "sy",
        client_secret_ref: null,
        client_secret_set: true,
        scopes: "openid",
        username_claim: "preferred_username",
        email_claim: "email",
        groups_claim: "groups",
      },
      {
        id: "i2",
        protocol: "saml",
        name: "Entra ID",
        enabled: false,
        version: 1,
        created_at: "2026-10-01T00:00:00Z",
        warnings: [],
        idp_metadata_url: null,
        idp_entity_id: "https://sts.windows.net/x/",
        sso_url: "https://login/x",
        signing_certificates: [],
        sp_entity_id: "sp",
        name_id_format: null,
        sign_requests: false,
        sp_signing_key_ref: null,
        sp_signing_certificate: null,
        attribute_username: "u",
        attribute_email: "e",
        attribute_groups: "g",
        acs_url: "https://sy/acs",
        sp_metadata_url: "https://sy/meta",
      },
    ];
    let bg = {
      enabled: false,
      enabled_until: null as string | null,
      enabled_by: null as string | null,
      reason: null as string | null,
      forced_by_deployment: false,
      admin: {
        username: "breakglass",
        totp_confirmed: true,
        created_at: "2026-10-02T00:00:00Z",
        last_login_at: null,
        locked_until: null,
      },
    };
    server.use(
      http.get("*/api/v1/identity-providers", () => HttpResponse.json(IDP)),
      http.get("*/api/v1/role-mappings", () =>
        HttpResponse.json([{ provider_id: "i1", tenant_id: "t1", tenant: "prod" }]),
      ),
      http.patch("*/api/v1/identity-providers/:id", async ({ request, params }) => {
        calls[`idp-${String(params.id)}`] = {
          body: await request.json(),
          ifMatch: request.headers.get("if-match"),
        };
        return HttpResponse.json(IDP[0]);
      }),
      http.get("*/api/v1/system/break-glass", () => HttpResponse.json(bg)),
      http.put("*/api/v1/system/break-glass", async ({ request }) => {
        const b = (await request.json()) as { enabled: boolean; reason?: string };
        calls.bg = b;
        bg = {
          ...bg,
          enabled: b.enabled,
          enabled_until: b.enabled ? new Date(Date.now() + 3 * 3_600_000).toISOString() : null,
          enabled_by: "Miguel",
          reason: b.reason ?? null,
        };
        return HttpResponse.json(bg);
      }),
    );
    renderAt("/admin/identity-providers");
    expect(await screen.findByText("1 in prod")).toBeInTheDocument();
    expect(screen.getByText("SAML 2.0")).toBeInTheDocument();
    const entra = screen.getByText("Entra ID").closest("tr")!;
    await userEvent.click(within(entra).getByRole("button", { name: "Enable" }));
    await waitFor(() =>
      expect(calls["idp-i2"]).toEqual({ body: { protocol: "saml", enabled: true }, ifMatch: "1" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Authentik" }));
    const dlg = within(await screen.findByRole("dialog"));
    expect(dlg.getByText("groups claim missing")).toBeInTheDocument();
    const cid = dlg.getByLabelText("Client ID");
    await userEvent.clear(cid);
    await userEvent.type(cid, "sy2");
    await userEvent.click(dlg.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(calls["idp-i1"]).toEqual({
        body: { protocol: "oidc", name: "Authentik", client_id: "sy2" },
        ifMatch: "3",
      }),
    );
    await userEvent.type(await screen.findByLabelText("Reason"), "upgrade window");
    await userEvent.click(screen.getByRole("button", { name: "Turn on" }));
    await waitFor(() =>
      expect(calls.bg).toEqual({ enabled: true, reason: "upgrade window", ttl_minutes: 240 }),
    );
    expect(await screen.findByText(/On · [23] h/)).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Turn off now" }));
    await waitFor(() => expect(calls.bg).toEqual({ enabled: false }));
  });

  it("enrols a replacement break-glass administrator and confirms the code", async () => {
    let confirmed: unknown = null;
    server.use(
      http.get("*/api/v1/identity-providers", () => HttpResponse.json([])),
      http.get("*/api/v1/role-mappings", () => HttpResponse.json([])),
      http.get("*/api/v1/system/break-glass", () =>
        HttpResponse.json({
          enabled: false,
          enabled_until: null,
          enabled_by: null,
          reason: null,
          forced_by_deployment: false,
          admin: null,
        }),
      ),
      http.post("*/api/v1/system/break-glass/admin", () =>
        HttpResponse.json({
          username: "breakglass",
          password: "pw-once",
          totp_secret: "SECRET",
          otpauth_uri: "otpauth://x",
        }),
      ),
      http.post("*/api/v1/system/break-glass/admin/confirm", async ({ request }) => {
        confirmed = await request.json();
        return HttpResponse.json({});
      }),
    );
    renderAt("/admin/identity-providers");
    await userEvent.click(await screen.findByRole("button", { name: "Create administrator" }));
    expect(await screen.findByText("pw-once")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Authenticator code"), "123456");
    const confirmBtn = screen.getByRole("button", { name: "Confirm" });
    expect(confirmBtn).toBeDisabled();
    await userEvent.click(screen.getByLabelText("I have stored it"));
    await userEvent.click(confirmBtn);
    await waitFor(() => expect(confirmed).toEqual({ totp: "123456" }));
  });

  it("adds a SAML provider, tests sign-in, and deletes a provider", async () => {
    const OIDC = {
      id: "i1",
      protocol: "oidc",
      name: "Authentik",
      enabled: true,
      version: 3,
      created_at: "2026-10-01T00:00:00Z",
      warnings: [],
      issuer_url: "https://auth/",
      client_id: "sy",
      client_secret_ref: null,
      client_secret_set: true,
      scopes: "openid",
      username_claim: "u",
      email_claim: "e",
      groups_claim: "g",
    };
    let created: unknown = null;
    let removed = false;
    const opened: string[] = [];
    const origOpen = window.open;
    window.open = ((u: string) => {
      opened.push(u);
      return null;
    }) as typeof window.open;
    server.use(
      http.get("*/api/v1/identity-providers", () => HttpResponse.json([OIDC])),
      http.get("*/api/v1/role-mappings", () => HttpResponse.json([])),
      http.get("*/api/v1/system/break-glass", () =>
        HttpResponse.json({
          enabled: false,
          enabled_until: null,
          enabled_by: null,
          reason: null,
          forced_by_deployment: true,
          admin: null,
        }),
      ),
      http.post("*/api/v1/identity-providers", async ({ request }) => {
        created = await request.json();
        return HttpResponse.json(OIDC, { status: 201 });
      }),
      http.post("*/api/v1/identity-providers/i1/test", () =>
        HttpResponse.json({ test_id: "tt", authorize_url: "https://auth/authorize?x=1" }),
      ),
      http.get("*/api/v1/identity-providers/i1/test/tt", () =>
        HttpResponse.json({
          status: "done",
          username: "sam",
          groups: ["g1"],
          would_grant: [{ role: "viewer", tenant: "prod" }],
          error: null,
        }),
      ),
      http.delete("*/api/v1/identity-providers/i1", () => {
        removed = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    try {
      renderAt("/admin/identity-providers");
      expect(await screen.findByText(/forces break-glass on/)).toBeInTheDocument();
      await userEvent.click(await screen.findByRole("button", { name: "Add provider" }));
      const dlg = within(await screen.findByRole("dialog"));
      await userEvent.click(dlg.getByRole("radio", { name: "SAML 2.0" }));
      await userEvent.type(dlg.getByLabelText("Name"), "Entra");
      await userEvent.type(dlg.getByLabelText("IdP metadata URL"), "https://m/xml");
      await userEvent.click(dlg.getByRole("button", { name: "Add provider" }));
      await waitFor(() =>
        expect(created).toEqual({
          protocol: "saml",
          name: "Entra",
          idp_metadata_url: "https://m/xml",
          enabled: true,
        }),
      );
      await userEvent.click(await screen.findByRole("button", { name: "Authentik" }));
      const edit = within(await screen.findByRole("dialog"));
      await userEvent.click(edit.getByRole("button", { name: "Start test" }));
      expect(await edit.findByText("sam")).toBeInTheDocument();
      expect(opened).toEqual(["https://auth/authorize?x=1"]);
      await userEvent.click(edit.getByRole("button", { name: "Delete" }));
      await userEvent.click(await screen.findByRole("button", { name: "Delete provider" }));
      await waitFor(() => expect(removed).toBe(true));
    } finally {
      window.open = origOpen;
    }
  });

  it("shows the server's reason when creating a tenant fails", async () => {
    server.use(
      http.post("*/api/v1/tenants", () =>
        HttpResponse.json(
          {
            title: "Validation failed",
            status: 422,
            errors: [{ path: "name", code: "x", message: "Name already exists." }],
          },
          { status: 422 },
        ),
      ),
    );
    renderAt("/admin/tenants");
    await userEvent.click(await screen.findByRole("button", { name: "Create tenant" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Name"), "globius-lab");
    await userEvent.click(dlg.getByRole("button", { name: "Create tenant" }));
    expect(await dlg.findByText("Name already exists.")).toBeInTheDocument();
  });
});
