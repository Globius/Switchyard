import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { commonCandidates } from "@/features/certificates/RequestCertificate";
import { renewalSummary } from "@/features/certificates/RenewalHistory";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const PRESETS = [
  {
    key: "letsencrypt",
    label: "Let's Encrypt",
    directory_url: "https://acme-v02.api.letsencrypt.org/directory",
    eab_required: false,
    auth_mode: "none",
    note: "Free, publicly trusted.",
  },
  {
    key: "zerossl",
    label: "ZeroSSL",
    directory_url: "https://acme.zerossl.com/v2/DV90",
    eab_required: true,
    auth_mode: "eab",
    note: "Needs External Account Binding.",
  },
  {
    key: "custom",
    label: "Custom",
    directory_url: null,
    eab_required: false,
    auth_mode: "none",
    note: "Any ACME v2 directory.",
  },
];
const PROVIDER = (over: Record<string, unknown> = {}) => ({
  id: "p1",
  name: "zerossl",
  preset: "zerossl",
  directory_url: "https://acme.zerossl.com/v2/DV90",
  email: "pki@example.test",
  auth_mode: "eab",
  eab_kid: "kHq3",
  eab_configured: true,
  oauth: null,
  oauth_secret_set: false,
  ca_bundle_set: false,
  account_registered: true,
  default: false,
  enabled: true,
  terms_agreed: true,
  used_by: 2,
  version: 1,
  ...over,
});
const TYPES = [
  {
    type: "cloudflare",
    label: "Cloudflare",
    supports_zone_discovery: true,
    fields: [
      {
        key: "api_token",
        label: "API token",
        kind: "text",
        secret: true,
        required: true,
        help: "Zone:Read, DNS:Edit",
        default: null,
        options: [],
      },
    ],
  },
  {
    type: "route53",
    label: "Route 53",
    supports_zone_discovery: false,
    fields: [
      {
        key: "access_key_id",
        label: "Access key ID",
        kind: "text",
        secret: true,
        required: true,
        help: "",
        default: null,
        options: [],
      },
      {
        key: "role_arn",
        label: "Role ARN",
        kind: "text",
        secret: false,
        required: false,
        help: "Leave empty to use the key directly.",
        default: null,
        options: [],
      },
    ],
  },
];

let role: "tenant-admin" | "operator" | "system-admin" = "system-admin";
let providers: ReturnType<typeof PROVIDER>[] = [];
let creds: Record<string, unknown>[] = [];
let body: Record<string, unknown> | null = null;
let patched: Record<string, unknown> | null = null;
let removed = false;
const server = setupServer(
  http.get("*/api/v1/me", () =>
    HttpResponse.json({
      auth: "session",
      break_glass: false,
      csrf_token: "tok",
      email: null,
      id: "s",
      is_system: role === "system-admin",
      name: "Miguel",
      principal: "Miguel",
      session: null,
      tenants: [
        { id: "t1", name: "acme", role: role === "system-admin" ? "tenant-admin" : role, via: [] },
      ],
      features: { lxc_guests: false },
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "manage", object: "dns_provider", min_role: "tenant-admin", description: "DNS." },
        { verb: "manage", object: "certificate", min_role: "operator", description: "Certs." },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/acme-provider-presets", () => HttpResponse.json(PRESETS)),
  http.get("*/api/v1/acme-providers", () => HttpResponse.json(providers)),
  http.post("*/api/v1/acme-providers", async ({ request }) => {
    body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json(PROVIDER({ name: "le" }), { status: 201 });
  }),
  http.patch("*/api/v1/acme-providers/:id", async ({ request }) => {
    patched = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json(PROVIDER());
  }),
  http.delete("*/api/v1/acme-providers/:id", () => {
    removed = true;
    return new HttpResponse(null, { status: 204 });
  }),
  http.post("*/api/v1/acme-providers/:id/test", () =>
    HttpResponse.json({
      ok: false,
      detail: "The CA refused the key.",
      external_account_required: true,
      tos_url: "https://ca.example/tos",
      stages: [
        { name: "directory", ok: true, detail: "Directory reachable" },
        { name: "eab", ok: false, detail: "External account binding rejected" },
      ],
    }),
  ),
  http.get("*/api/v1/dns-provider-types", () => HttpResponse.json(TYPES)),
  http.get("*/api/v1/tenants/acme/dns-providers", () => HttpResponse.json(creds)),
  http.post("*/api/v1/tenants/acme/dns-providers", async ({ request }) => {
    body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ ...body, id: "d9" }, { status: 201 });
  }),
  http.patch("*/api/v1/tenants/acme/dns-providers/:id", async ({ request }) => {
    patched = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json(creds[0]);
  }),
  http.delete("*/api/v1/tenants/acme/dns-providers/:id", () => {
    removed = true;
    return new HttpResponse(null, { status: 204 });
  }),
  http.post("*/api/v1/tenants/acme/dns-providers/:id/test", () =>
    HttpResponse.json({
      ok: false,
      detail: "",
      zones: [
        { zone: "example.test", ok: true, detail: "TXT created, seen by resolvers, removed" },
        { zone: "example.net", ok: false, detail: "Access denied" },
      ],
    }),
  ),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  role = "system-admin";
  providers = [];
  creds = [];
  body = null;
  patched = null;
  removed = false;
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

describe("helpers", () => {
  it("finds the credentials that cover every name", () => {
    const c = (id: string) => ({ id, name: id, type: "cloudflare", zone: "z" });
    expect(
      commonCandidates([
        { name: "a.example.test", candidates: [c("x"), c("y")] },
        { name: "b.example.test", candidates: [c("y")] },
      ]).map((x) => x.id),
    ).toEqual(["y"]);
    expect(commonCandidates([])).toEqual([]);
    expect(commonCandidates([{ name: "a", candidates: [] }])).toEqual([]);
  });

  it("summarises renewal for the list", () => {
    const base = {
      certificate: "c",
      certificate_id: "1",
      failures_in_a_row: 0,
      issuer: "zerossl",
      last_attempt_at: null,
      last_error: null,
      last_ok: null,
      last_success_at: null,
      not_after: "2026-12-21T00:00:00Z",
      renew_before_days: 30,
      renew_due_at: "2026-11-21T00:00:00Z",
      renew_policy: "auto" as const,
      source: "acme",
    };
    expect(renewalSummary({ ...base, renew_policy: "manual" }).line).toBe("Manual");
    const ok = renewalSummary({ ...base, last_ok: true, last_success_at: "2026-09-22T00:00:00Z" });
    expect(ok).toMatchObject({ failed: false, line: "Auto · 30 days before" });
    expect(ok.sub).toMatch(/renewed .* · next about/);
    expect(
      renewalSummary({ ...base, last_ok: false, last_attempt_at: "2026-10-11T08:00:00Z" }).failed,
    ).toBe(true);
  });
});

describe("ACME providers", () => {
  it("lists providers and adds one with the terms accepted", async () => {
    providers = [PROVIDER()];
    renderAt("/admin/acme-providers");
    expect(await screen.findByText("zerossl")).toBeInTheDocument();
    expect(screen.getByText("Registered")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add provider" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Name"), "le");
    await userEvent.type(dlg.getByLabelText("Contact email"), "pki@example.test");
    const add = dlg.getByRole("button", { name: "Add provider" });
    expect(add).toBeDisabled();
    await userEvent.click(dlg.getByRole("checkbox"));
    await userEvent.click(add);
    await waitFor(() =>
      expect(body).toMatchObject({
        name: "le",
        preset: "letsencrypt",
        email: "pki@example.test",
        terms_agreed: true,
        auth_mode: "none",
      }),
    );
    expect(body).not.toHaveProperty("account_key", "x");
  });

  it("edits with write-only secrets, sends only what changed, and shows the test result", async () => {
    providers = [PROVIDER()];
    renderAt("/admin/acme-providers");
    await userEvent.click(await screen.findByRole("button", { name: "zerossl" }));
    const dlg = within(await screen.findByRole("dialog"));
    expect(dlg.getByText("Used by 2 certificates, so it cannot be deleted")).toBeInTheDocument();
    expect(dlg.getByRole("button", { name: "Delete" })).toBeDisabled();
    await userEvent.click(dlg.getByRole("button", { name: "Replace EAB HMAC key" }));
    expect(dlg.getByText("EAB HMAC key (replacing)")).toBeInTheDocument();
    await userEvent.type(dlg.getByLabelText("EAB HMAC key"), "newsecret");
    await userEvent.click(dlg.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patched).toEqual({ eab_hmac_key: "newsecret" }));
  });

  it("tests the connection stage by stage and can delete an unused provider", async () => {
    providers = [PROVIDER({ used_by: 0 })];
    renderAt("/admin/acme-providers");
    await userEvent.click(await screen.findByRole("button", { name: "zerossl" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.click(dlg.getByRole("button", { name: "Test connection" }));
    expect(await dlg.findByText(/Test failed/)).toBeInTheDocument();
    expect(dlg.getByText("Directory reachable")).toBeInTheDocument();
    expect(dlg.getByText("External account binding rejected")).toBeInTheDocument();
    expect(dlg.getByRole("link", { name: "https://ca.example/tos" })).toBeInTheDocument();
    await userEvent.click(dlg.getByRole("button", { name: "Delete" }));
    await userEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Delete provider",
      }),
    );
    await waitFor(() => expect(removed).toBe(true));
  });
});

describe("DNS credentials", () => {
  it("adds a credential from the provider's own fields, with a write-only secret and zones", async () => {
    role = "tenant-admin";
    renderAt("/t/acme/dns-credentials");
    await userEvent.click(await screen.findByRole("button", { name: "Add credential" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Name"), "cf-example");
    await userEvent.type(dlg.getByLabelText("API token"), "tok123");
    await userEvent.type(
      dlg.getByRole("combobox", { name: "Zones this credential covers" }),
      "example.test,",
    );
    await userEvent.click(dlg.getByRole("button", { name: "Add credential" }));
    await waitFor(() =>
      expect(body).toMatchObject({
        name: "cf-example",
        type: "cloudflare",
        zones: ["example.test"],
        credentials: { api_token: "tok123" },
      }),
    );
  });

  it("locks the provider on edit, keeps stored secrets unless replaced, and tests per zone", async () => {
    role = "tenant-admin";
    creds = [
      {
        id: "d1",
        name: "r53",
        type: "route53",
        zones: ["example.test"],
        enabled: true,
        credentials_set: true,
        config: { role_arn: "arn:aws:iam::1:role/x" },
        version: 1,
      },
    ];
    renderAt("/t/acme/dns-credentials");
    await userEvent.click(await screen.findByRole("button", { name: "r53" }));
    const dlg = within(await screen.findByRole("dialog"));
    expect(dlg.getByRole("radio", { name: "Route 53" })).toBeChecked();
    expect(dlg.getByRole("radio", { name: "Cloudflare" })).toBeDisabled();
    expect(dlg.getByText("Set")).toBeInTheDocument();
    await userEvent.click(dlg.getByRole("button", { name: "Test" }));
    expect(await dlg.findByText(/Test failed/)).toBeInTheDocument();
    expect(dlg.getByText(/TXT created, seen by resolvers, removed/)).toBeInTheDocument();
    expect(dlg.getByText(/Access denied/)).toBeInTheDocument();
    await userEvent.click(dlg.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(patched).toMatchObject({ zones: ["example.test"], credentials: null }),
    );
  });

  it("puts a zone clash on the zones field", async () => {
    role = "tenant-admin";
    server.use(
      http.post("*/api/v1/tenants/acme/dns-providers", () =>
        HttpResponse.json(
          {
            title: "Invalid",
            status: 422,
            errors: [
              {
                path: "zones",
                code: "zone_taken",
                message: "example.test is claimed by cf-example",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    renderAt("/t/acme/dns-credentials");
    await userEvent.click(await screen.findByRole("button", { name: "Add credential" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Name"), "again");
    await userEvent.type(dlg.getByLabelText("API token"), "t");
    await userEvent.click(dlg.getByRole("button", { name: "Add credential" }));
    expect((await dlg.findAllByText(/claimed by cf-example/)).length).toBeGreaterThan(0);
  });

  it("is read-only for an operator", async () => {
    role = "operator";
    creds = [];
    renderAt("/t/acme/dns-credentials");
    await screen.findByText("No DNS credentials yet");
    expect(screen.getByRole("button", { name: "Add credential" })).toBeDisabled();
  });
});
