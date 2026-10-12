import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const PROVIDERS = [
  { id: "p1", name: "letsencrypt-prod", preset: "letsencrypt", enabled: true, default: true },
  { id: "p2", name: "zerossl", preset: "zerossl", enabled: true, default: false },
  { id: "p3", name: "off", preset: "custom", enabled: false, default: false },
];
const CERT = (id: string, name: string, source: string) => ({
  id,
  tenant_id: "t1",
  name,
  sans: [`${name}.example.test`],
  not_after: "2027-01-01T00:00:00Z",
  purpose: "server",
  ocsp_stapling: false,
  renew_policy: "auto",
  renew_before_days: 30,
  secret_ref: "x",
  crl_secret_ref: null,
  source,
  version: 1,
  draft_status: "unchanged",
  changed_fields: [],
});
const CAND = (id: string, name: string, zone: string) => ({ id, name, type: "cloudflare", zone });

let issued: Record<string, unknown> | null = null;
let match: Record<string, { name: string; candidates: ReturnType<typeof CAND>[] }> = {};
let problems: { path: string; message: string }[] = [];
let jobState: "failed" | "succeeded" = "failed";
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
      tenants: [{ id: "t1", name: "acme", role: "operator", via: [] }],
      features: { lxc_guests: false },
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "manage", object: "certificate", min_role: "operator", description: "Certs." },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/acme-providers", () => HttpResponse.json(PROVIDERS)),
  http.get("*/api/v1/tenants/acme/dns-providers/match", ({ request }) => {
    const names = new URL(request.url).searchParams.getAll("names");
    return HttpResponse.json(names.map((n) => match[n] ?? { name: n, candidates: [] }));
  }),
  http.post("*/api/v1/tenants/acme/certificates/issue/check", () =>
    HttpResponse.json({ ok: problems.length === 0, problems }),
  ),
  http.post("*/api/v1/tenants/acme/certificates/issue", async ({ request }) => {
    issued = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ job_id: "j" }, { status: 202 });
  }),
  http.get("*/api/v1/tenants/acme/jobs/j", () =>
    HttpResponse.json({
      id: "j",
      kind: "issue_cert",
      state: jobState,
      attempts: 1,
      max_attempts: 1,
      retrying: false,
      created_at: "2026-10-11T10:00:00Z",
      change_set_id: jobState === "succeeded" ? "cs9" : null,
      error:
        jobState === "failed" ? "urn:ietf:params:acme:error:dns: SERVFAIL looking up TXT" : null,
      stages: [
        { name: "order", state: "succeeded" },
        { name: "validate", state: jobState === "failed" ? "failed" : "succeeded" },
        { name: "finalize", state: jobState === "failed" ? "pending" : "succeeded" },
      ],
    }),
  ),
  http.get("*/api/v1/tenants/acme/certificates", () =>
    HttpResponse.json({
      items: [CERT("c1", "api", "acme"), CERT("c2", "sso", "acme"), CERT("c3", "up", "upload")],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/tenants/acme/certificate-renewals", () =>
    HttpResponse.json([
      {
        certificate: "api",
        certificate_id: "c1",
        failures_in_a_row: 2,
        issuer: "letsencrypt-prod",
        last_attempt_at: "2026-10-11T08:00:00Z",
        last_error: "CA said no",
        last_ok: false,
        last_success_at: null,
        not_after: "2027-01-01T00:00:00Z",
        renew_before_days: 30,
        renew_due_at: "2026-12-02T00:00:00Z",
        renew_policy: "auto",
        source: "acme",
      },
      {
        certificate: "sso",
        certificate_id: "c2",
        failures_in_a_row: 0,
        issuer: "zerossl",
        last_attempt_at: "2026-09-22T00:00:00Z",
        last_error: null,
        last_ok: true,
        last_success_at: "2026-09-22T00:00:00Z",
        not_after: "2027-01-01T00:00:00Z",
        renew_before_days: 30,
        renew_due_at: "2026-12-02T00:00:00Z",
        renew_policy: "auto",
        source: "acme",
      },
    ]),
  ),
  http.get("*/api/v1/tenants/acme/certificates/c1/renewals", () =>
    HttpResponse.json([
      {
        id: "r1",
        certificate: "api",
        acme_provider: "letsencrypt-prod",
        challenge: "dns-01",
        trigger: "auto",
        ok: false,
        error: "urn:ietf:params:acme:error:rateLimited",
        started_at: "2026-10-11T08:00:00Z",
        finished_at: "2026-10-11T08:01:00Z",
        not_after: null,
        change_set_id: null,
      },
    ]),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  issued = null;
  match = {};
  problems = [];
  jobState = "failed";
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

async function openRequest() {
  renderAt("/t/acme/certificates");
  const open = await screen.findByRole("button", { name: "Request certificate" });
  await waitFor(() => expect(open).toBeEnabled());
  await userEvent.click(open);
  return within(await screen.findByRole("dialog"));
}

describe("request certificate", () => {
  it("preselects the default provider and the longest-zone credential, then requests with DNS-01", async () => {
    match = {
      "docs.example.test": {
        name: "docs.example.test",
        candidates: [
          CAND("d2", "cf-docs", "docs.example.test"),
          CAND("d1", "cf-example", "example.test"),
        ],
      },
      "www.example.test": {
        name: "www.example.test",
        candidates: [CAND("d1", "cf-example", "example.test")],
      },
    };
    const dlg = await openRequest();
    expect(await dlg.findByRole("combobox", { name: "ACME provider" })).toHaveTextContent(
      "letsencrypt-prod (default)",
    );
    await userEvent.type(dlg.getByLabelText("Name"), "docs");
    await userEvent.type(
      dlg.getByRole("combobox", { name: "Names (SANs)" }),
      "docs.example.test,www.example.test,",
    );
    await waitFor(() =>
      expect(dlg.getByRole("combobox", { name: "DNS credential" })).toHaveTextContent(
        "cf-example · example.test",
      ),
    );
    await userEvent.click(dlg.getByRole("button", { name: "Request" }));
    await waitFor(() =>
      expect(issued).toMatchObject({
        name: "docs",
        source: "acme",
        acme_provider: "letsencrypt-prod",
        challenge: "dns-01",
        dns_provider: "cf-example",
        sans: ["docs.example.test", "www.example.test"],
        renew_policy: "auto",
        renew_before_days: 30,
      }),
    );
  });

  it("blocks the request when no credential covers a name", async () => {
    match = { "docs.example.test": { name: "docs.example.test", candidates: [] } };
    const dlg = await openRequest();
    await userEvent.type(dlg.getByLabelText("Name"), "docs");
    await userEvent.type(dlg.getByRole("combobox", { name: "Names (SANs)" }), "docs.example.test,");
    expect(
      await dlg.findByText(/No credential of this tenant covers: docs\.example\.test/),
    ).toBeInTheDocument();
    expect(dlg.getByRole("button", { name: "Request" })).toBeDisabled();
    expect(dlg.getByText(/Add a DNS credential that covers every name/)).toBeInTheDocument();
  });

  it("shows the server's findings and keeps Request disabled", async () => {
    problems = [{ path: "sans[0]", message: "Name does not resolve to a VIP of this tenant" }];
    const dlg = await openRequest();
    await userEvent.click(dlg.getByRole("radio", { name: "HTTP-01" }));
    await userEvent.type(dlg.getByLabelText("Name"), "docs");
    await userEvent.type(dlg.getByRole("combobox", { name: "Names (SANs)" }), "docs.example.test,");
    expect(await dlg.findByText(/does not resolve to a VIP/)).toBeInTheDocument();
    expect(dlg.getByRole("button", { name: "Request" })).toBeDisabled();
    expect(dlg.queryByRole("combobox", { name: "DNS credential" })).toBeNull();
  });

  it("quotes the CA's error on failure and offers Retry, Edit request and Open job", async () => {
    match = {
      "docs.example.test": {
        name: "docs.example.test",
        candidates: [CAND("d1", "cf-example", "example.test")],
      },
    };
    const dlg = await openRequest();
    await userEvent.type(dlg.getByLabelText("Name"), "docs");
    await userEvent.type(dlg.getByRole("combobox", { name: "Names (SANs)" }), "docs.example.test,");
    await waitFor(() => expect(dlg.getByRole("button", { name: "Request" })).toBeEnabled());
    await userEvent.click(dlg.getByRole("button", { name: "Request" }));
    expect(await screen.findByText(/SERVFAIL looking up TXT/)).toBeInTheDocument();
    expect(screen.getByText("Challenge published and validated by the CA")).toBeInTheDocument();
    expect(screen.getByText("not reached")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open job" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Edit request" }));
    expect(await screen.findByLabelText("Name")).toHaveValue("docs");
  });

  it("links the change set when the request succeeds", async () => {
    jobState = "succeeded";
    match = {
      "docs.example.test": {
        name: "docs.example.test",
        candidates: [CAND("d1", "cf-example", "example.test")],
      },
    };
    const dlg = await openRequest();
    await userEvent.type(dlg.getByLabelText("Name"), "docs");
    await userEvent.type(dlg.getByRole("combobox", { name: "Names (SANs)" }), "docs.example.test,");
    await waitFor(() => expect(dlg.getByRole("button", { name: "Request" })).toBeEnabled());
    await userEvent.click(dlg.getByRole("button", { name: "Request" }));
    expect(await screen.findByRole("link", { name: "Open the change set" })).toBeInTheDocument();
  });
});

describe("renewal in the certificate list", () => {
  it("shows the issuer, the renewal state and the history with the CA's error", async () => {
    renderAt("/t/acme/certificates");
    const api = (await screen.findByText("api")).closest("tr")!;
    expect(within(api).getByText("Renewal failed")).toBeInTheDocument();
    expect(within(api).getByText("letsencrypt-prod")).toBeInTheDocument();
    const sso = screen.getByText("sso").closest("tr")!;
    expect(within(sso).getByText(/renewed .* · next about/)).toBeInTheDocument();
    await userEvent.click(within(api).getByRole("button", { name: "History" }));
    const dlg = within(await screen.findByRole("dialog"));
    expect(await dlg.findByText(/rateLimited/)).toBeInTheDocument();
    expect(dlg.getByText("2 failed attempts in a row.")).toBeInTheDocument();
    expect(dlg.getByRole("button", { name: "Retry renewal now" })).toBeEnabled();
  });
});
