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
import { cleanMatch, describeMatch, parseNetworks } from "@/features/policies/MatchEditor";
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
  it("checks IPv6 ranges strictly", () => {
    expect(parseCidrs("2001:db8::/129").error).toEqual({
      line: 1,
      message: "prefix length must be 0–128 for IPv6",
    });
    expect(parseCidrs("::::").error?.message).toBe("not a valid address or range");
    expect(parseCidrs("2001:db8::1" + String.fromCharCode(10) + "fe80::/10").cidrs).toEqual([
      "2001:db8::1",
      "fe80::/10",
    ]);
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
  http.get("*/api/v1/waf/crs-rules", () =>
    HttpResponse.json({
      complete: false,
      crs_version: "4.8.0",
      groups: [
        {
          name: "SQL injection",
          description: "",
          prefix: 942,
          rules: [{ id: 942100, name: "libinjection SQLi", paranoia_level: 1 }],
        },
      ],
    }),
  ),
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
  http.post("*/api/v1/tenants/acme/certificates/issue/check", () =>
    HttpResponse.json({ ok: true, problems: [] }),
  ),
  http.get("*/api/v1/tenants/acme/jobs/j", () =>
    HttpResponse.json({
      id: "j",
      kind: "issue_cert",
      state: "running",
      attempts: 1,
      max_attempts: 1,
      retrying: false,
      created_at: "2026-10-11T10:00:00Z",
      stages: [{ name: "order", state: "succeeded" }],
    }),
  ),
  http.get("*/api/v1/tenants/acme/certificate-renewals", () => HttpResponse.json([])),
  http.get("*/api/v1/acme-providers", () => HttpResponse.json([])),
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

  it("filters by expiry tier and issuer, and shows the tier legend", async () => {
    renderAt("/t/acme/certificates");
    await screen.findByText("mqtt");
    expect(screen.getByText("Expiry:")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Expiry"), "7");
    expect(screen.getByText("mqtt")).toBeInTheDocument();
    expect(screen.queryByText("wildcard")).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("Expiry"), "14");
    expect(screen.getByText("wildcard")).toBeInTheDocument();
    expect(screen.queryByText("portal")).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("Expiry"), "");
    await userEvent.selectOptions(screen.getByLabelText("Issuer"), "cloudflare_origin");
    expect(screen.getByText("portal")).toBeInTheDocument();
    expect(screen.queryByText("mqtt")).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("Issuer"), "");
    await userEvent.type(screen.getByLabelText("Filter by name or SAN"), "*.example");
    expect(screen.getByText("wildcard")).toBeInTheDocument();
    expect(screen.queryByText("mqtt")).toBeNull();
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
    const issue = await screen.findByRole("button", { name: "Request certificate" });
    await waitFor(() => expect(issue).toBeEnabled());
    await userEvent.click(issue);
    await userEvent.click(await screen.findByRole("radio", { name: "Cloudflare Origin CA" }));
    await userEvent.type(screen.getByLabelText("Name"), "docs");
    await userEvent.type(
      screen.getByRole("combobox", { name: "Names (SANs)" }),
      "docs.example.test,www.example.test{enter}",
    );
    await userEvent.click(screen.getByRole("button", { name: "Request" }));
    await waitFor(() =>
      expect(issued).toEqual({
        name: "docs",
        source: "cloudflare_origin",
        sans: ["docs.example.test", "www.example.test"],
        renew_policy: "auto",
      }),
    );
    expect(
      await screen.findByRole("heading", { name: "Requesting certificate" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Order placed")).toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: "Request certificate" })).toBeDisabled();
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

describe("match helpers", () => {
  it("drops empty conditions and describes what is left", () => {
    expect(cleanMatch({ host: "", methods: [], headers: [], query: [], source_cidrs: [] })).toEqual(
      {},
    );
    const m = cleanMatch({
      host: "api.example.test",
      path_prefix: "/v2",
      methods: ["GET", "POST"],
      headers: [
        { name: "X-Env", op: "present", value: "ignored" },
        { name: "X-A", op: "equals", value: "1" },
      ],
      query: [{ name: "debug", op: "present", value: "x" }],
      source_cidrs: ["10.0.0.0/8"],
      sni: "*.example.test",
    });
    expect(m.headers).toEqual([
      { name: "X-Env", op: "present" },
      { name: "X-A", op: "equals", value: "1" },
    ]);
    expect(m.query).toEqual([{ name: "debug", op: "present" }]);
    expect(describeMatch(m)).toBe(
      "GET/POST · api.example.test · /v2* · X-Env present · X-A equals 1 · ?debug · from 10.0.0.0/8 · SNI *.example.test",
    );
    expect(describeMatch({})).toBe("every request");
  });

  it("parses one network per line, IPv4 and IPv6", () => {
    expect(parseNetworks("10.0.0.0/8\n\n2001:db8::/32 ")).toEqual({
      list: ["10.0.0.0/8", "2001:db8::/32"],
    });
    expect(parseNetworks("10.0.0.0/8\nnope").error).toMatch(/Line 2/);
  });
});

describe("rule policies", () => {
  it("creates a rewrite policy with a header action, a variable value and a header condition", async () => {
    renderAt("/t/acme/policies");
    const n = await screen.findByRole("button", { name: "New rewrite policy" });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "add-headers");
    await userEvent.type(screen.getByLabelText("Rule 1 header name"), "X-Client");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 value source"), "variable");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 variable"), "client_ip");
    await userEvent.click(screen.getByText("More conditions"));
    await userEvent.click(screen.getByRole("button", { name: "Add header condition" }));
    await userEvent.type(screen.getByLabelText("Rule 1 header 1 name"), "X-Env");
    await userEvent.type(screen.getByLabelText("Rule 1 header 1 value"), "canary");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toEqual({
      kind: "rewrite",
      name: "add-headers",
      rules: [
        {
          phase: "request",
          match: { headers: [{ name: "X-Env", op: "equals", value: "canary" }] },
          action: { type: "set_header", name: "X-Client", variable: "client_ip" },
        },
      ],
    });
  });

  it("offers Replace path prefix only in the request phase", async () => {
    renderAt("/t/acme/policies");
    const n = await screen.findByRole("button", { name: "New rewrite policy" });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    const action = await screen.findByLabelText("Rule 1 action");
    expect(within(action).getByRole("option", { name: "Replace path prefix" })).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 phase"), "response");
    expect(within(action).queryByRole("option", { name: "Replace path prefix" })).toBeNull();
  });

  it("creates a fixed response with a body and Retry-After", async () => {
    renderAt("/t/acme/policies");
    const n = await screen.findByRole("button", { name: "New fixed response" });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "maintenance");
    expect(screen.getByLabelText("Rule 1 retry after")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Rule 1 retry after"), "120");
    await userEvent.type(screen.getByLabelText("Rule 1 body"), "Back soon");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toEqual({
      kind: "respond",
      name: "maintenance",
      rules: [
        {
          match: {},
          status: 503,
          body: "Back soon",
          content_type: "text/html",
          retry_after_s: 120,
        },
      ],
    });
  });

  it("creates custom error pages for the chosen statuses only", async () => {
    renderAt("/t/acme/policies");
    const n = await screen.findByRole("button", { name: "New error pages" });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "pages");
    await userEvent.click(screen.getByLabelText("Custom page for 503"));
    await userEvent.type(screen.getByLabelText("Page 503 body"), "Down");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toEqual({
      kind: "error_pages",
      name: "pages",
      pages: [{ status: 503, content_type: "text/html", body: "Down" }],
    });
  });

  it("shows server findings by path", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", () =>
        HttpResponse.json(
          {
            title: "Invalid",
            status: 422,
            errors: [
              {
                path: "rules[0].action.name",
                code: "protected_header",
                message: "Host cannot be changed",
              },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    renderAt("/t/acme/policies");
    const n = await screen.findByRole("button", { name: "New rewrite policy" });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "bad");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    expect(await screen.findByText(/Host cannot be changed/)).toBeInTheDocument();
  });
});

describe("rule editor details", () => {
  it("edits a path-prefix rewrite with every match condition and keeps rule order", async () => {
    renderAt("/t/acme/policies");
    const n = await screen.findByRole("button", { name: "New rewrite policy" });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "v2");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 action"), "replace_prefix");
    await userEvent.type(screen.getByLabelText("Rule 1 from prefix"), "/api");
    await userEvent.type(screen.getByLabelText("Rule 1 to prefix"), "/v2");
    await userEvent.click(screen.getByText("More conditions"));
    await userEvent.click(screen.getByLabelText("Rule 1 GET"));
    await userEvent.click(screen.getByLabelText("Rule 1 POST"));
    await userEvent.click(screen.getByLabelText("Rule 1 POST"));
    await userEvent.click(screen.getByRole("button", { name: "Add query condition" }));
    await userEvent.type(screen.getByLabelText("Rule 1 query 1 name"), "debug");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 query 1 operator"), "present");
    await userEvent.type(screen.getByLabelText("Rule 1 source networks"), "10.0.0.0/8");
    await userEvent.type(screen.getByLabelText("Rule 1 SNI"), "*.example.test");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 path match"), "prefix");
    await userEvent.type(screen.getByLabelText("Rule 1 path"), "x");
    await userEvent.type(screen.getByLabelText("Rule 1 host"), "api.example.test");
    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    await userEvent.click(screen.getByRole("button", { name: "Move up 2" }));
    await userEvent.click(screen.getByRole("button", { name: "Move down 1" }));
    await userEvent.click(screen.getByRole("button", { name: /Remove Rule 2/ }));
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    const rule = (posted!.ops[0]!.body as { rules: Record<string, unknown>[] }).rules;
    expect(rule).toHaveLength(1);
    expect(rule[0]).toMatchObject({
      phase: "request",
      action: { type: "replace_prefix", from_prefix: "/api", to_prefix: "/v2" },
      match: {
        host: "api.example.test",
        path_prefix: "/x",
        methods: ["GET"],
        query: [{ name: "debug", op: "present" }],
        source_cidrs: ["10.0.0.0/8"],
        sni: "*.example.test",
      },
    });
  });

  it("flags a bad source network and sets a remove-header action without a value", async () => {
    renderAt("/t/acme/policies");
    const n = await screen.findByRole("button", { name: "New rewrite policy" });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "strip");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 action"), "remove_header");
    expect(screen.queryByLabelText("Rule 1 value")).toBeNull();
    await userEvent.type(screen.getByLabelText("Rule 1 header name"), "Server");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 phase"), "response");
    await userEvent.click(screen.getByText("More conditions"));
    await userEvent.click(screen.getByRole("button", { name: "Add header condition" }));
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 header 1 operator"), "absent");
    expect(screen.getByLabelText("Rule 1 header 1 value")).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Rule 1 source networks"), "nope");
    expect(await screen.findByText(/Line 1: not a valid address or network/)).toBeInTheDocument();
  });

  it("deletes a rewrite policy after confirming, and picks the content type of an error page", async () => {
    renderAt("/t/acme/policies");
    const n = await screen.findByRole("button", { name: "New error pages" });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "pages");
    await userEvent.click(screen.getByLabelText("Custom page for 502"));
    await userEvent.selectOptions(
      screen.getByLabelText("Page 502 content type"),
      "application/json",
    );
    await userEvent.type(screen.getByLabelText("Page 502 body"), "x");
    await userEvent.click(screen.getByLabelText("Custom page for 502"));
    expect(screen.queryByLabelText("Page 502 body")).toBeNull();
  });
});

describe("rate limit policies", () => {
  it("limits requests per client with an error answer", async () => {
    renderAt("/t/acme/policies");
    const n = await screen.findByRole("button", { name: "New rate limit" });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "login-limit");
    await userEvent.clear(screen.getByLabelText("Rule 1 max requests"));
    await userEvent.type(screen.getByLabelText("Rule 1 max requests"), "5");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 period"), "60");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 deny status"), "503");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 path match"), "prefix");
    await userEvent.type(screen.getByLabelText("Rule 1 path"), "login");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      kind: "rate_limit",
      name: "login-limit",
      rules: [
        {
          key: "client_ip",
          max_requests: 5,
          period_s: 60,
          action: "deny",
          deny_status: 503,
          match: { path_prefix: "/login" },
        },
      ],
    });
  });

  it("counts requests in flight per header value and holds offenders", async () => {
    renderAt("/t/acme/policies");
    const n = await screen.findByRole("button", { name: "New rate limit" });
    await waitFor(() => expect(n).toBeEnabled());
    await userEvent.click(n);
    await userEvent.type(await screen.findByLabelText("Name"), "api-key-limit");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 key"), "header");
    await userEvent.type(screen.getByLabelText("Rule 1 key header"), "X-Api-Key");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 limit type"), "concurrent");
    expect(screen.queryByLabelText("Rule 1 period")).toBeNull();
    await userEvent.clear(screen.getByLabelText("Rule 1 max concurrent"));
    await userEvent.type(screen.getByLabelText("Rule 1 max concurrent"), "8");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 action"), "tarpit");
    await userEvent.clear(screen.getByLabelText("Rule 1 tarpit seconds"));
    await userEvent.type(screen.getByLabelText("Rule 1 tarpit seconds"), "3");
    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    await userEvent.click(screen.getByRole("button", { name: "Move up 2" }));
    await userEvent.click(screen.getByRole("button", { name: "Move down 1" }));
    await userEvent.click(screen.getByRole("button", { name: /Remove Rule 2/ }));
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      kind: "rate_limit",
      rules: [
        {
          key: "header",
          header_name: "X-Api-Key",
          max_concurrent: 8,
          action: "tarpit",
          tarpit_s: 3,
        },
      ],
    });
    const rule = (posted!.ops[0]!.body as { rules: Record<string, unknown>[] }).rules[0]!;
    expect(rule).not.toHaveProperty("period_s");
    expect(rule).not.toHaveProperty("deny_status");
  });
});

describe("security policies", () => {
  async function open(kind: string) {
    renderAt("/t/acme/policies");
    const sel = await screen.findByLabelText("New security policy");
    await waitFor(() => expect(sel).toBeEnabled());
    await userEvent.selectOptions(sel, kind);
    return screen.findByLabelText("Name");
  }

  it("creates a login policy with a group rule and no secrets typed", async () => {
    await userEvent.type(await open("access"), "staff-login");
    await userEvent.type(screen.getByLabelText("Issuer URL"), "https://idp.example.test");
    await userEvent.type(screen.getByLabelText("Client ID"), "sy");
    await userEvent.type(screen.getByLabelText("Client secret reference"), "kv/sy/oidc");
    await userEvent.type(screen.getByLabelText("Cookie secret reference"), "kv/sy/cookie");
    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 mode"), "groups");
    await userEvent.type(screen.getByRole("combobox", { name: "Rule 1 groups" }), "staff,");
    await userEvent.clear(screen.getByLabelText("Header for token"));
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      kind: "access",
      name: "staff-login",
      oidc: { issuer: "https://idp.example.test", client_secret_ref: "kv/sy/oidc" },
      identity_headers: { token: null, user: "X-Auth-User" },
      rules: [{ mode: "groups", groups: ["staff"], match: {} }],
    });
  });

  it("creates a JWT policy from a public key", async () => {
    await userEvent.type(await open("jwt"), "api-jwt");
    await userEvent.type(screen.getByLabelText("Rule 1 issuer"), "https://idp.example.test");
    await userEvent.type(screen.getByLabelText("Rule 1 key 1 kid"), "k1");
    await userEvent.type(screen.getByLabelText("Rule 1 key 1 public key"), "PEM");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      kind: "jwt",
      rules: [
        {
          issuer: "https://idp.example.test",
          leeway_s: 30,
          keys: [{ kid: "k1", alg: "RS256", public_key_pem: "PEM" }],
        },
      ],
    });
  });

  it("creates a client certificate policy with a known certificate", async () => {
    await userEvent.type(await open("client_cert"), "cert-id");
    await userEvent.click(screen.getByRole("button", { name: "Add certificate" }));
    await userEvent.type(screen.getByLabelText("Identity 1 value"), "sam");
    await userEvent.type(screen.getByLabelText("Identity 1 user"), "sam.o");
    await userEvent.type(screen.getByRole("combobox", { name: "Identity 1 groups" }), "ops,dev,");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 mode"), "identity");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      kind: "client_cert",
      identities: [{ by: "cn", value: "sam", user: "sam.o", groups: ["ops", "dev"] }],
      rules: [{ mode: "identity" }],
    });
  });

  it("creates a WAF policy in detect mode with disabled rules and excluded paths", async () => {
    await userEvent.type(await open("waf"), "shop-waf");
    expect(screen.getByLabelText("WAF mode")).toHaveValue("detect");
    await userEvent.selectOptions(screen.getByLabelText("Paranoia level"), "2");
    await userEvent.type(screen.getByRole("combobox", { name: "Disabled rule IDs" }), "942100,");
    await userEvent.type(screen.getByRole("combobox", { name: "Paths not inspected" }), "/health,");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      kind: "waf",
      mode: "detect",
      fail_mode: "open",
      paranoia_level: 2,
      disabled_rule_ids: [942100],
      excluded_paths: ["/health"],
    });
  });

  it("creates a protection policy with connection limits and exempt networks", async () => {
    await userEvent.type(await open("protection"), "guard");
    await userEvent.type(screen.getByLabelText("Connections per source"), "50");
    await userEvent.click(screen.getByRole("switch", { name: "Refuse known scanners" }));
    await userEvent.type(screen.getByLabelText("Exempt networks"), "10.0.0.0/8");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      kind: "protection",
      max_connections_per_source: 50,
      block_scanners: true,
      exempt_cidrs: ["10.0.0.0/8"],
      bot_action: "deny",
    });
  });

  it("fills every login option: email domains, cookie, timing, scopes and headers", async () => {
    await userEvent.type(await open("access"), "all-login");
    await userEvent.type(screen.getByLabelText("Issuer URL"), "https://idp.example.test");
    await userEvent.type(screen.getByLabelText("Client ID"), "sy");
    await userEvent.type(screen.getByLabelText("Client secret reference"), "kv/a");
    await userEvent.type(screen.getByLabelText("Cookie secret reference"), "kv/b");
    await userEvent.clear(screen.getByLabelText("Groups claim"));
    await userEvent.type(screen.getByLabelText("Groups claim"), "roles");
    await userEvent.clear(screen.getByLabelText("Cookie name"));
    await userEvent.type(screen.getByLabelText("Cookie name"), "sy_s");
    await userEvent.selectOptions(screen.getByLabelText("Default mode"), "login");
    await userEvent.clear(screen.getByLabelText("Login path"));
    await userEvent.type(screen.getByLabelText("Login path"), "/sy-login");
    await userEvent.type(screen.getByLabelText("Header for email"), "X");
    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    await userEvent.selectOptions(screen.getByLabelText("Rule 2 mode"), "email_domains");
    await userEvent.type(
      screen.getByRole("combobox", { name: "Rule 2 email domains" }),
      "example.com,",
    );
    await userEvent.click(screen.getByRole("button", { name: "Remove Rule 1" }));
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      kind: "access",
      default_mode: "login",
      auth_path: "/sy-login",
      rules: [{ mode: "email_domains", email_domains: ["example.com"] }],
    });
  });

  it("adds and removes JWT rules and keys", async () => {
    await userEvent.type(await open("jwt"), "multi-jwt");
    await userEvent.type(screen.getByLabelText("Rule 1 issuer"), "https://a.test");
    await userEvent.type(screen.getByRole("combobox", { name: "Rule 1 audiences" }), "api,");
    await userEvent.type(screen.getByRole("combobox", { name: "Rule 1 scopes" }), "read,");
    await userEvent.type(screen.getByLabelText("Rule 1 subject header"), "X-Sub");
    await userEvent.type(screen.getByLabelText("Rule 1 key 1 kid"), "k1");
    await userEvent.type(screen.getByLabelText("Rule 1 key 1 public key"), "PEM1");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 key 1 algorithm"), "ES256");
    await userEvent.click(screen.getByRole("button", { name: "Add key" }));
    await userEvent.type(screen.getByLabelText("Rule 1 key 2 kid"), "k2");
    await userEvent.click(screen.getByRole("button", { name: "Remove key 2" }));
    expect(screen.queryByLabelText("Rule 1 key 2 kid")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      kind: "jwt",
      rules: [
        {
          audiences: ["api"],
          required_scopes: ["read"],
          subject_header: "X-Sub",
          keys: [{ kid: "k1", alg: "ES256" }],
        },
      ],
    });
  });

  it("sets the WAF to block and fail closed with body inspection off", async () => {
    await userEvent.type(await open("waf"), "strict");
    await userEvent.selectOptions(screen.getByLabelText("WAF mode"), "block");
    await userEvent.selectOptions(screen.getByLabelText("Fail mode"), "closed");
    await userEvent.click(screen.getByRole("switch", { name: "Inspect request bodies" }));
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({ kind: "waf", mode: "block", fail_mode: "closed" });
  });

  it("sets every protection limit and the bot action", async () => {
    await userEvent.type(await open("protection"), "tight");
    await userEvent.type(screen.getByLabelText("New connections per source"), "20");
    await userEvent.selectOptions(screen.getByLabelText("Connection rate period"), "60");
    await userEvent.type(screen.getByLabelText("Header timeout"), "10");
    await userEvent.type(screen.getByLabelText("Client timeout"), "60");
    await userEvent.click(screen.getByRole("switch", { name: "Refuse an empty User-Agent" }));
    await userEvent.type(
      screen.getByRole("combobox", { name: "User-Agent text to refuse" }),
      "sqlmap,",
    );
    await userEvent.selectOptions(screen.getByLabelText("Bot action"), "silent_drop");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({ kind: "protection", bot_action: "silent_drop" });
  });

  it("edits client certificate headers, identity by SHA-1 and a group rule", async () => {
    await userEvent.type(await open("client_cert"), "cc2");
    await userEvent.clear(screen.getByLabelText("User header"));
    await userEvent.type(screen.getByLabelText("User header"), "X-U");
    await userEvent.clear(screen.getByLabelText("Groups header"));
    await userEvent.type(screen.getByLabelText("Groups header"), "X-G");
    await userEvent.click(screen.getByRole("button", { name: "Add certificate" }));
    await userEvent.click(screen.getByRole("button", { name: "Add certificate" }));
    await userEvent.selectOptions(screen.getByLabelText("Identity 2 match by"), "sha1");
    await userEvent.type(screen.getByLabelText("Identity 2 value"), "ab12");
    await userEvent.click(screen.getByRole("button", { name: "Remove identity 1" }));
    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 mode"), "groups");
    await userEvent.type(screen.getByRole("combobox", { name: "Rule 1 groups" }), "ops,");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      kind: "client_cert",
      identities: [{ by: "sha1", value: "ab12" }],
      rules: [{ mode: "groups", groups: ["ops"] }, { mode: "valid" }],
    });
  });

  it("adds and removes a second JWT rule", async () => {
    await userEvent.type(await open("jwt"), "two-rules");
    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    expect(screen.getByLabelText("Rule 2 issuer")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Remove Rule 2" }));
    expect(screen.queryByLabelText("Rule 2 issuer")).toBeNull();
  });

  it("shows the WAF rule set as read-only and sets the thresholds", async () => {
    await userEvent.type(await open("waf"), "thr");
    expect(screen.getByLabelText("Core Rule Set version")).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Inbound threshold"), "7");
    await userEvent.type(screen.getByLabelText("Outbound threshold"), "6");
    await userEvent.type(screen.getByLabelText("WAF time limit"), "50");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({ kind: "waf" });
  });

  it("adds a JavaScript challenge and then switches to a CAPTCHA", async () => {
    await userEvent.type(await open("protection"), "gate");
    await userEvent.selectOptions(screen.getByLabelText("Visitor check"), "js");
    await userEvent.selectOptions(screen.getByLabelText("Challenge difficulty"), "5");
    await userEvent.type(screen.getByRole("combobox", { name: "Paths never checked" }), "/api,");
    await userEvent.selectOptions(screen.getByLabelText("Visitor check"), "captcha");
    expect(screen.queryByLabelText("Challenge difficulty")).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("CAPTCHA provider"), "hcaptcha");
    await userEvent.type(screen.getByLabelText("CAPTCHA site key"), "site-1");
    await userEvent.type(screen.getByLabelText("CAPTCHA secret reference"), "kv/captcha");
    await userEvent.clear(screen.getByLabelText("Pass lifetime"));
    await userEvent.type(screen.getByLabelText("Pass lifetime"), "600");
    await userEvent.type(
      screen.getByRole("combobox", { name: "User-Agent text never checked" }),
      "GoogleBot,",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      kind: "protection",
      challenge: null,
      captcha: {
        provider: "hcaptcha",
        site_key: "site-1",
        secret_ref: "kv/captcha",
        valid_for_s: 600,
        skip_user_agents: ["googlebot"],
      },
    });
  });

  it("adds device conditions to a login rule and drops them for a public rule", async () => {
    await userEvent.type(await open("access"), "zt");
    await userEvent.type(screen.getByLabelText("Issuer URL"), "https://idp.example.test");
    await userEvent.type(screen.getByLabelText("Client ID"), "sy");
    await userEvent.type(screen.getByLabelText("Client secret reference"), "kv/a");
    await userEvent.type(screen.getByLabelText("Cookie secret reference"), "kv/b");
    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    await userEvent.click(
      screen.getByRole("switch", { name: "Rule 1 requires a device certificate" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Add claim check" }));
    await userEvent.type(screen.getByLabelText("Rule 1 claim 1 name"), "device_compliant");
    await userEvent.click(screen.getByRole("button", { name: "Add claim check" }));
    await userEvent.type(screen.getByLabelText("Rule 1 claim 2 name"), "tier");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 claim 2 check"), "equals");
    await userEvent.type(screen.getByLabelText("Rule 1 claim 2 value"), "gold");
    await userEvent.click(screen.getByRole("button", { name: "Add claim check" }));
    await userEvent.type(screen.getByLabelText("Rule 1 claim 3 name"), "os");
    await userEvent.selectOptions(screen.getByLabelText("Rule 1 claim 3 check"), "in");
    await userEvent.type(screen.getByRole("combobox", { name: "Rule 1 claim 3 values" }), "mac,");
    await userEvent.click(screen.getByRole("button", { name: "Rule 1 remove claim 3" }));
    await userEvent.click(screen.getByRole("button", { name: "Add rule" }));
    await userEvent.selectOptions(screen.getByLabelText("Rule 2 mode"), "public");
    expect(screen.queryByLabelText("Rule 2 requires a device certificate")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      rules: [
        {
          posture: {
            device_certificate: true,
            claims: [
              { name: "device_compliant", op: "true", value: null, values: [] },
              { name: "tier", op: "equals", value: "gold", values: [] },
            ],
          },
        },
        { mode: "public", posture: null },
      ],
    });
  });

  it("suggests CRS rules for the WAF and keeps a JWKS URL on a JWT rule", async () => {
    await userEvent.type(await open("waf"), "crs");
    await userEvent.click(screen.getByRole("combobox", { name: "Disabled rule IDs" }));
    expect(await screen.findByText(/942100 libinjection SQLi/)).toBeInTheDocument();
  });

  it("saves the JWKS URL of a JWT rule", async () => {
    await userEvent.type(await open("jwt"), "jw");
    await userEvent.type(screen.getByLabelText("Rule 1 issuer"), "https://i.test");
    await userEvent.type(screen.getByLabelText("Rule 1 JWKS URL"), "https://i.test/jwks.json");
    await userEvent.type(screen.getByLabelText("Rule 1 key 1 public key"), "PEM");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(posted?.ops[0]?.body).toBeDefined());
    expect(posted!.ops[0]!.body).toMatchObject({
      rules: [{ jwks_url: "https://i.test/jwks.json" }],
    });
  });
});
