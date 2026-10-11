import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { buildBundle, looksLikePem, toBase64 } from "@/features/certificates/Certificates";
import { parseCidrs } from "@/features/policies/Policies";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const days = (n: number) => new Date(Date.now() + n * 86_400_000 + 3_600_000).toISOString();
const CERT = (id: string, name: string, extra: object = {}) => ({
  id,
  name,
  source: "upload",
  sans: ["a.example.test"],
  not_after: days(41),
  renew_policy: "manual",
  secret_ref: "kv/x",
  version: 3,
  draft_status: "unchanged",
  changed_fields: [],
  tenant_id: "t1",
  ...extra,
});
const CERT_ONLY = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----";
const KEY_ONLY = "-----BEGIN PRIVATE KEY-----\ndef\n-----END PRIVATE KEY-----";
async function fillPem() {
  await userEvent.click(screen.getByLabelText("Certificate"));
  await userEvent.paste(CERT_ONLY);
  await userEvent.click(screen.getByLabelText("Private key"));
  await userEvent.paste(KEY_ONLY);
}
const PEM =
  "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n-----BEGIN PRIVATE KEY-----\ndef\n-----END PRIVATE KEY-----";

describe("helpers", () => {
  it("recognises PEM blocks", () => {
    expect(looksLikePem(PEM)).toEqual({ cert: true, key: true });
    expect(looksLikePem("-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----")).toEqual({
      cert: true,
      key: false,
    });
    expect(looksLikePem("hello")).toEqual({ cert: false, key: false });
  });
  it("parses CIDR lines with the line number of the first problem", () => {
    expect(parseCidrs("203.0.113.0/24\n\n10.0.0.1\n2001:db8::/32")).toEqual({
      cidrs: ["203.0.113.0/24", "10.0.0.1", "2001:db8::/32"],
    });
    expect(parseCidrs("10.0.0.0/24\n10.40.0.0/33").error).toEqual({
      line: 2,
      message: "prefix length must be 0–32 for IPv4",
    });
    expect(parseCidrs("nope").error?.message).toBe("not a valid address or range");
    expect(parseCidrs("").error?.message).toBe("add at least one range");
  });
});

let role = "operator";
let posted: {
  ops: {
    action: string;
    object_id: string;
    body?: Record<string, unknown>;
    expected_version?: number;
  }[];
} | null = null;
let imported: { body: unknown; query: string } | null = null;
let issued: unknown = null;
let renewed: string | null = null;
let importStatus = 202;
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
      tenants: [{ id: "t1", name: "acme", role, via: [] }],
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "write", object: "application", min_role: "operator", description: "Change apps." },
        {
          verb: "manage",
          object: "certificate",
          min_role: "operator",
          description: "Manage certificates.",
        },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
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
  http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", async ({ request }) => {
    posted = (await request.json()) as typeof posted;
    return HttpResponse.json({ id: "cs1" });
  }),
  http.get("*/api/v1/tenants/acme/certificates", () =>
    HttpResponse.json({
      items: [
        CERT("c2", "wildcard", { not_after: days(12), sans: ["*.example.test"] }),
        CERT("c1", "mqtt", { not_after: days(5) }),
        CERT("c3", "portal", { source: "cloudflare_origin", renew_policy: "auto" }),
        CERT("c4", "gone", { draft_status: "deleted" }),
      ],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services", () =>
    HttpResponse.json({
      items: [
        {
          id: "v1",
          name: "api-gateway",
          vip: "10.0.0.1",
          ports: [{ port: 443, tls: true }],
          draft_status: "unchanged",
          certificate_ids: ["c2"],
          policy_set_ids: ["pol1"],
          allow_list_id: "pol2",
          default_pool_id: null,
          routes: [],
        },
      ],
      next_cursor: null,
    }),
  ),
  http.post("*/api/v1/tenants/acme/certificates/import", async ({ request }) => {
    imported = { body: await request.json(), query: new URL(request.url).search };
    return importStatus === 202
      ? HttpResponse.json({ change_set_id: "cs1", object_id: "x" }, { status: 202 })
      : HttpResponse.json(
          { title: "Service unavailable", status: 503, detail: "OpenBao is not configured." },
          { status: 503 },
        );
  }),
  http.post("*/api/v1/tenants/acme/certificates/issue", async ({ request }) => {
    issued = await request.json();
    return HttpResponse.json({ change_set_id: "cs1", job_id: "j" }, { status: 202 });
  }),
  http.post("*/api/v1/tenants/acme/certificates/:id/renew", ({ params }) => {
    renewed = String(params.id);
    return HttpResponse.json({ change_set_id: "cs1", job_id: "j" }, { status: 202 });
  }),
  http.get("*/api/v1/tenants/acme/policy-sets", () =>
    HttpResponse.json({
      items: [
        {
          id: "pol1",
          name: "legacy-redirects",
          kind: "redirect",
          rules: [
            { match: { host: "old.test" }, redirect: { location: "https://new.test", code: 301 } },
            { match: { path_prefix: "/a" }, redirect: { location: "/b", code: 308 } },
          ],
          version: 2,
          draft_status: "unchanged",
          changed_fields: [],
          tenant_id: "t1",
        },
        {
          id: "pol2",
          name: "office-ranges",
          kind: "allow_list",
          cidrs: ["203.0.113.0/24"],
          managed_by: null,
          version: 5,
          draft_status: "modified",
          changed_fields: [],
          tenant_id: "t1",
        },
        {
          id: "pol3",
          name: "cloudflare-ips",
          kind: "allow_list",
          cidrs: ["173.245.48.0/20"],
          managed_by: "cloudflare",
          version: 1,
          draft_status: "unchanged",
          changed_fields: [],
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
  posted = null;
  imported = null;
  issued = null;
  renewed = null;
  importStatus = 202;
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

describe("certificates", () => {
  it("lists certificates sorted by expiry with warning icons, renewal and used-by", async () => {
    renderAt("/t/acme/certificates");
    await screen.findByText("mqtt");
    const names = screen
      .getAllByRole("row")
      .slice(1)
      .map((r) => within(r).getAllByRole("cell")[0]!.textContent);
    expect(names.slice(0, 2)).toEqual(["mqtt", "wildcard"]);
    const mqtt = screen.getByText("mqtt").closest("tr")!;
    expect(within(mqtt).getByText("5 days")).toBeInTheDocument();
    const wild = screen.getByText("wildcard").closest("tr")!;
    expect(within(wild).getByText("12 days")).toBeInTheDocument();
    expect(await within(wild).findByText("api-gateway")).toBeInTheDocument();
    expect(
      within(screen.getByText("portal").closest("tr")!).getByText("Auto · 30 days before"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByText("portal").closest("tr")!).getByText("Cloudflare Origin CA"),
    ).toBeInTheDocument();
    expect(screen.getByText("gone")).toHaveClass("line-through");
  });

  it("delete is blocked while a service uses the certificate; otherwise adds a delete op", async () => {
    renderAt("/t/acme/certificates");
    const wild = (await screen.findByText("wildcard")).closest("tr")!;
    await waitFor(() =>
      expect(within(wild).getByRole("button", { name: "Delete" })).toBeDisabled(),
    );
    const mqtt = screen.getByText("mqtt").closest("tr")!;
    await waitFor(() => expect(within(mqtt).getByRole("button", { name: "Delete" })).toBeEnabled());
    await userEvent.click(within(mqtt).getByRole("button", { name: "Delete" }));
    await userEvent.click(await screen.findByRole("button", { name: "Add delete to draft" }));
    await waitFor(() =>
      expect(posted?.ops[0]).toMatchObject({
        action: "delete",
        object_id: "c1",
        expected_version: 3,
      }),
    );
  });

  it("imports PEM into the draft; the PEM is sent once and the button needs both blocks", async () => {
    renderAt("/t/acme/certificates");
    const imp = await screen.findByRole("button", { name: "Import" });
    await waitFor(() => expect(imp).toBeEnabled());
    await userEvent.click(imp);
    const add = await screen.findByRole("button", { name: "Add to draft" });
    expect(add).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Name"), "status");
    await fillPem();
    await waitFor(() => expect(add).toBeEnabled());
    await userEvent.click(add);
    await waitFor(() =>
      expect(imported?.body).toEqual({ name: "status", pem: `${CERT_ONLY}\n${KEY_ONLY}\n` }),
    );
    expect(imported?.query).toBe("?change_set=cs1");
  });

  it("opens a draft first when there is none, then imports into it", async () => {
    let opened = false;
    server.use(
      http.get("*/api/v1/tenants/acme/change-sets", () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
      http.post("*/api/v1/tenants/acme/change-sets", () => {
        opened = true;
        return HttpResponse.json(
          {
            id: "cs9",
            state: "draft",
            author: "me",
            summary: "x",
            base_revision: 1,
            created_at: "2026-10-10T10:00:00Z",
            ops: [],
          },
          { status: 201 },
        );
      }),
    );
    renderAt("/t/acme/certificates");
    const imp = await screen.findByRole("button", { name: "Import" });
    await waitFor(() => expect(imp).toBeEnabled());
    await userEvent.click(imp);
    await userEvent.type(await screen.findByLabelText("Name"), "status");
    await fillPem();
    await userEvent.click(screen.getByRole("button", { name: "Add to draft" }));
    await waitFor(() => expect(imported?.query).toBe("?change_set=cs9"));
    expect(opened).toBe(true);
  });

  it("joins certificate, chain and key into one bundle in order; the chain is optional", async () => {
    expect(buildBundle(" A ", " B ", " C ")).toBe("A\nB\nC\n");
    expect(buildBundle("A", "", "C")).toBe("A\nC\n");
    renderAt("/t/acme/certificates");
    const imp = await screen.findByRole("button", { name: "Import" });
    await waitFor(() => expect(imp).toBeEnabled());
    await userEvent.click(imp);
    await userEvent.type(await screen.findByLabelText("Name"), "status");
    const add = screen.getByRole("button", { name: "Add to draft" });
    await userEvent.click(screen.getByLabelText("Certificate"));
    await userEvent.paste(CERT_ONLY);
    expect(add).toBeDisabled();
    await userEvent.click(screen.getByLabelText("Private key"));
    await userEvent.paste("not a key");
    expect(add).toBeDisabled();
    await userEvent.clear(screen.getByLabelText("Private key"));
    await userEvent.click(screen.getByLabelText("Private key"));
    await userEvent.paste(KEY_ONLY);
    await userEvent.click(screen.getByLabelText("Chain (optional)"));
    await userEvent.paste("junk");
    expect(add).toBeDisabled();
    await userEvent.clear(screen.getByLabelText("Chain (optional)"));
    await userEvent.click(screen.getByLabelText("Chain (optional)"));
    const CHAIN = CERT_ONLY.replace("abc", "chain");
    await userEvent.paste(CHAIN);
    await waitFor(() => expect(add).toBeEnabled());
    await userEvent.click(add);
    await waitFor(() =>
      expect((imported?.body as { pem: string }).pem).toBe(`${CERT_ONLY}\n${CHAIN}\n${KEY_ONLY}\n`),
    );
  });

  it("fills a field from a chosen file", async () => {
    renderAt("/t/acme/certificates");
    const imp = await screen.findByRole("button", { name: "Import" });
    await waitFor(() => expect(imp).toBeEnabled());
    await userEvent.click(imp);
    const input = (await screen.findByLabelText("Private key file")) as HTMLInputElement;
    await userEvent.upload(input, new File([KEY_ONLY], "k.pem", { type: "text/plain" }));
    await waitFor(() => expect(screen.getByLabelText("Private key")).toHaveValue(KEY_ONLY));
  });

  it("clicking a certificate shows where it is used, with links to the services", async () => {
    renderAt("/t/acme/certificates");
    await userEvent.click(await screen.findByRole("button", { name: "Where wildcard is used" }));
    expect(await screen.findByText("Used by 1 virtual services")).toBeInTheDocument();
    const link = await screen.findByRole("link", { name: "api-gateway" });
    expect(link).toHaveAttribute("href", "/t/acme/virtual-services/v1");
    expect(screen.getByText("kv/x")).toBeInTheDocument();
  });

  it("replaces an uploaded certificate's material in the draft, keeping its id", async () => {
    const replaced: { body: unknown; query: string }[] = [];
    server.use(
      http.post("*/api/v1/tenants/acme/certificates/:id/replace", async ({ request }) => {
        replaced.push({ body: await request.json(), query: new URL(request.url).search });
        return HttpResponse.json({ change_set_id: "cs1", object_id: "c1" }, { status: 202 });
      }),
    );
    renderAt("/t/acme/certificates");
    await userEvent.click(await screen.findByRole("button", { name: "Where wildcard is used" }));
    await userEvent.click(await screen.findByRole("button", { name: "Replace" }));
    expect(screen.queryByLabelText("Name")).not.toBeInTheDocument();
    await fillPem();
    const go = await screen.findByRole("button", { name: "Replace in draft" });
    await waitFor(() => expect(go).toBeEnabled());
    await userEvent.click(go);
    await waitFor(() =>
      expect(replaced[0]?.body).toEqual({
        pem: `${CERT_ONLY}
${KEY_ONLY}
`,
      }),
    );
    expect(replaced[0]?.query).toBe("?change_set=cs1");
  });

  it("toggles OCSP stapling into the draft, and does not offer it for Cloudflare Origin CA", async () => {
    renderAt("/t/acme/certificates");
    await userEvent.click(await screen.findByRole("button", { name: "Where wildcard is used" }));
    const sw = await screen.findByRole("switch", { name: "OCSP stapling" });
    await waitFor(() => expect(sw).toBeEnabled());
    await userEvent.click(sw);
    await waitFor(() =>
      expect(posted?.ops[0]).toMatchObject({ action: "update", body: { ocsp_stapling: true } }),
    );
  });

  it("imports a client CA with an optional CRL into the draft", async () => {
    let body: unknown = null;
    server.use(
      http.post("*/api/v1/tenants/acme/certificates/import-ca", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ change_set_id: "cs1", object_id: "ca1" }, { status: 202 });
      }),
    );
    renderAt("/t/acme/certificates");
    const open = await screen.findByRole("button", { name: "Import client CA" });
    await waitFor(() => expect(open).toBeEnabled());
    await userEvent.click(open);
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Name"), "office-ca");
    await userEvent.type(dlg.getByLabelText("CA certificate"), CERT_ONLY);
    await userEvent.type(
      dlg.getByLabelText("CRL (optional)"),
      "-----BEGIN X509 CRL-----x-----END X509 CRL-----",
    );
    await userEvent.click(dlg.getByRole("button", { name: "Add to draft" }));
    await waitFor(() => expect(body).toMatchObject({ name: "office-ca", pem: CERT_ONLY }));
    expect((body as { crl_pem: string }).crl_pem).toContain("X509 CRL");
  });

  it("a certificate nobody uses says it can be deleted", async () => {
    renderAt("/t/acme/certificates");
    await userEvent.click(await screen.findByRole("button", { name: "Where mqtt is used" }));
    expect(await screen.findByText(/can be deleted/)).toBeInTheDocument();
  });

  it("imports a PKCS#12 file with its password, sending base64 once", async () => {
    expect(toBase64(new Uint8Array([0, 1, 2, 250, 255]))).toBe("AAEC+v8=");
    renderAt("/t/acme/certificates");
    const imp = await screen.findByRole("button", { name: "Import" });
    await waitFor(() => expect(imp).toBeEnabled());
    await userEvent.click(imp);
    await userEvent.type(await screen.findByLabelText("Name"), "bundle");
    await userEvent.click(screen.getByRole("button", { name: "PKCS#12 / PFX" }));
    const add = screen.getByRole("button", { name: "Add to draft" });
    expect(add).toBeDisabled();
    await userEvent.upload(
      screen.getByLabelText("PKCS#12 file"),
      new File([new Uint8Array([0, 1, 2, 250, 255])], "site.pfx"),
    );
    expect(await screen.findByText("Selected site.pfx")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Password"), "s3cret");
    await waitFor(() => expect(add).toBeEnabled());
    await userEvent.click(add);
    await waitFor(() =>
      expect(imported?.body).toEqual({
        name: "bundle",
        pkcs12_b64: "AAEC+v8=",
        password: "s3cret",
      }),
    );
  });

  it("shows the server's reason for a wrong PKCS#12 password", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/certificates/import", () =>
        HttpResponse.json(
          {
            title: "Validation failed",
            status: 422,
            detail: "the PKCS#12 password is wrong",
            errors: [{ path: "password", code: "bad_password", message: "Wrong password." }],
          },
          { status: 422 },
        ),
      ),
    );
    renderAt("/t/acme/certificates");
    const imp = await screen.findByRole("button", { name: "Import" });
    await waitFor(() => expect(imp).toBeEnabled());
    await userEvent.click(imp);
    await userEvent.type(await screen.findByLabelText("Name"), "bundle");
    await userEvent.click(screen.getByRole("button", { name: "PKCS#12 / PFX" }));
    await userEvent.upload(
      screen.getByLabelText("PKCS#12 file"),
      new File([new Uint8Array([1])], "a.p12"),
    );
    await userEvent.click(screen.getByRole("button", { name: "Add to draft" }));
    expect(await screen.findByText(/Wrong password/)).toBeInTheDocument();
  });

  it("shows why an import failed", async () => {
    importStatus = 503;
    renderAt("/t/acme/certificates");
    const imp = await screen.findByRole("button", { name: "Import" });
    await waitFor(() => expect(imp).toBeEnabled());
    await userEvent.click(imp);
    await userEvent.type(await screen.findByLabelText("Name"), "x");
    await fillPem();
    await userEvent.click(screen.getByRole("button", { name: "Add to draft" }));
    expect(await screen.findByText("OpenBao is not configured.")).toBeInTheDocument();
  });

  it("issues a certificate through a job and renews a non-upload one", async () => {
    renderAt("/t/acme/certificates");
    const issue = await screen.findByRole("button", { name: "Issue certificate" });
    await waitFor(() => expect(issue).toBeEnabled());
    await userEvent.click(issue);
    await userEvent.click(await screen.findByRole("radio", { name: "Cloudflare Origin CA" }));
    await userEvent.type(screen.getByLabelText("Name"), "docs");
    await userEvent.type(
      screen.getByRole("combobox", { name: "Names (SANs)" }),
      "docs.example.test,www.example.test{enter}",
    );
    await userEvent.click(screen.getByRole("button", { name: "Start issuing" }));
    await waitFor(() =>
      expect(issued).toEqual({
        name: "docs",
        source: "cloudflare_origin",
        sans: ["docs.example.test", "www.example.test"],
      }),
    );
    expect(await screen.findByText(/Issuing has started as a job/)).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Close" }).at(-1)!);
    const portal = screen.getByText("portal").closest("tr")!;
    expect(
      within(screen.getByText("mqtt").closest("tr")!).getByRole("button", { name: "Renew now" }),
    ).toBeDisabled();
    await userEvent.click(within(portal).getByRole("button", { name: "Renew now" }));
    await waitFor(() => expect(renewed).toBe("c3"));
  });

  it("viewers cannot import, issue or delete", async () => {
    role = "viewer";
    renderAt("/t/acme/certificates");
    await screen.findByText("mqtt");
    await waitFor(() => expect(screen.getByRole("button", { name: "Import" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Issue certificate" })).toBeDisabled();
  });
});

describe("policies", () => {
  it("lists redirect and allow-list policies with kind filter, managed badge and used-by", async () => {
    renderAt("/t/acme/policies");
    expect(await screen.findByText("Redirect · 2 rules", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Allow list · 1 CIDRs", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Allow list · managed", { exact: false })).toBeInTheDocument();
    expect(
      (await screen.findAllByText("api-gateway", { exact: false })).length,
    ).toBeGreaterThanOrEqual(2);
    await userEvent.click(screen.getByRole("button", { name: "Redirect" }));
    expect(screen.queryByText("office-ranges")).not.toBeInTheDocument();
  });

  it("edits a redirect policy: rules, code, order, and saves only rules", async () => {
    renderAt("/t/acme/policies");
    await userEvent.click(await screen.findByRole("button", { name: "legacy-redirects" }));
    const loc = await screen.findByLabelText("Rule 1 redirect to");
    await waitFor(() => expect(loc).toBeEnabled());
    await userEvent.clear(loc);
    await userEvent.type(loc, "https://newer.test");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 code"), "308");
    await userEvent.selectOptions(screen.getByLabelText("Rule 2 path match"), "exact");
    await userEvent.click(screen.getByRole("button", { name: "Move rule 2 up" }));
    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    await userEvent.click(screen.getByRole("button", { name: "Remove rule 3" }));
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() =>
      expect(posted?.ops[0]).toMatchObject({
        action: "update",
        object_id: "pol1",
        expected_version: 2,
      }),
    );
    expect(Object.keys(posted!.ops[0]!.body!)).toEqual(["rules"]);
  });

  it("validates allow-list CIDRs with a line number and saves the parsed list", async () => {
    renderAt("/t/acme/policies");
    await userEvent.click(await screen.findByRole("button", { name: "office-ranges" }));
    const ta = await screen.findByLabelText("Allowed source ranges");
    await waitFor(() => expect(ta).toBeEnabled());
    await userEvent.type(ta, "{enter}10.40.0.0/33");
    expect(
      await screen.findByText("Line 2: prefix length must be 0–32 for IPv4"),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    expect(posted).toBeNull();
    await userEvent.clear(ta);
    await userEvent.type(ta, "203.0.113.0/24{enter}198.51.100.16/28");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() =>
      expect(posted?.ops[0]?.body).toEqual({ cidrs: ["203.0.113.0/24", "198.51.100.16/28"] }),
    );
  });

  it("a Cloudflare-managed list is read-only", async () => {
    renderAt("/t/acme/policies");
    await userEvent.click(await screen.findByRole("button", { name: "cloudflare-ips" }));
    expect(await screen.findByText(/kept in sync with Cloudflare/)).toBeInTheDocument();
    expect(screen.getByLabelText("Allowed source ranges")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save to draft" })).not.toBeInTheDocument();
  });

  it("creates an allow list and deletes a policy after confirming", async () => {
    renderAt("/t/acme/policies");
    const n = await screen.findByRole("button", { name: "New allow list" });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "partners");
    await userEvent.type(screen.getByLabelText("Allowed source ranges"), "192.0.2.0/24");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() =>
      expect(posted?.ops[0]).toMatchObject({
        action: "create",
        body: { kind: "allow_list", name: "partners", cidrs: ["192.0.2.0/24"] },
      }),
    );
    posted = null;
    await userEvent.click(await screen.findByRole("button", { name: "legacy-redirects" }));
    const del = await screen.findByRole("button", { name: "Delete" });
    await waitFor(() => expect(del).toBeEnabled());
    await userEvent.click(del);
    expect(await screen.findByText(/1 services use this policy/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add delete to draft" }));
    await waitFor(() =>
      expect(posted?.ops[0]).toMatchObject({ action: "delete", object_id: "pol1" }),
    );
  });

  it("creates a redirect policy and maps a 422 onto its rule", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", () =>
        HttpResponse.json(
          {
            title: "Invalid",
            status: 422,
            detail: "x",
            errors: [
              {
                path: "rules.0.redirect.location",
                code: "bad",
                message: "Must be an absolute URL or a path.",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    renderAt("/t/acme/policies");
    const n = await screen.findByRole("button", { name: "New redirect policy" });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "r");
    await userEvent.type(screen.getByLabelText("Rule 1 redirect to"), "nope");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    expect(await screen.findByText(/Must be an absolute URL or a path/)).toBeInTheDocument();
  });

  it("viewers cannot create", async () => {
    role = "viewer";
    renderAt("/t/acme/policies");
    await screen.findByText("office-ranges");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "New allow list" })).toBeDisabled(),
    );
  });
});
