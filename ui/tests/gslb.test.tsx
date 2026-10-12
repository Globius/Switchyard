import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { stateTone } from "@/features/gslb/Gslb";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";
import { pick } from "./pick";

const ZONE = {
  id: "z1",
  name: "gslb.example.com",
  nameservers: ["ns1.example.com"],
  dnssec: false,
  version: 1,
  created_at: "2026-10-10T00:00:00Z",
  created_by: "x",
};
const WIDE = {
  id: "w1",
  name: "shop",
  zone: "gslb.example.com",
  fqdn: "shop.gslb.example.com",
  algorithm: "failover",
  ttl: 30,
  enabled: true,
  health: { kind: "tcp", port: 443, timeout_ms: 1000 },
  members: [{ address: "2001:db8::1", site: "pve1", priority: 1, weight: 1, subnets: [] }],
  version: 1,
  applied_at: "2026-10-12T10:00:00Z",
  last_error: null,
  created_at: "2026-10-10T00:00:00Z",
  created_by: "x",
};
let calls: { what: string; body?: unknown }[] = [];
let isSystem = false;
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
      tenants: [{ id: "t1", name: "acme", role: "tenant-admin", via: [] }],
      features: { lxc_guests: false },
    }),
  ),
  http.get("*/api/v1/permissions", () => HttpResponse.json({ roles: [], permissions: [] })),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/virtual-services", () =>
    HttpResponse.json({ items: [{ id: "v", name: "shop" }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/gslb-zones", () => HttpResponse.json([ZONE])),
  http.get("*/api/v1/tenants/acme/wide-ips", () => HttpResponse.json([WIDE])),
  http.get("*/api/v1/tenants/acme/gslb/status", () =>
    HttpResponse.json([{ fqdn: WIDE.fqdn, state: "drift", applied_at: null, last_error: null }]),
  ),
  http.post("*/api/v1/tenants/acme/gslb/sync", () => {
    calls.push({ what: "sync" });
    return HttpResponse.json({ written: 3, failed: 1 }, { status: 202 });
  }),
  http.post("*/api/v1/tenants/acme/gslb-zones/gslb.example.com/wide-ips", async ({ request }) => {
    calls.push({ what: "create", body: await request.json() });
    return HttpResponse.json(WIDE, { status: 201 });
  }),
  http.patch(
    "*/api/v1/tenants/acme/gslb-zones/gslb.example.com/wide-ips/shop",
    async ({ request }) => {
      calls.push({ what: "patch", body: await request.json() });
      return HttpResponse.json(WIDE);
    },
  ),
  http.delete("*/api/v1/tenants/acme/gslb-zones/gslb.example.com/wide-ips/shop", () => {
    calls.push({ what: "delete" });
    return new HttpResponse(null, { status: 204 });
  }),
  http.post("*/api/v1/tenants/acme/gslb-zones/gslb.example.com/dnssec", () => {
    calls.push({ what: "dnssec" });
    return HttpResponse.json({ zone: "gslb.example.com", dnssec: true, ds: ["1234 13 2 abcd"] });
  }),
  http.post("*/api/v1/tenants/acme/gslb-zones", async ({ request }) => {
    calls.push({ what: "zone", body: await request.json() });
    return HttpResponse.json(ZONE, { status: 201 });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  calls = [];
  isSystem = false;
});
afterAll(() => server.close());

function renderIt() {
  render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider
          router={createRouter({
            routeTree,
            history: createMemoryHistory({ initialEntries: ["/t/acme/gslb"] }),
          })}
        />
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("gslb", () => {
  it("maps states to tones", () => {
    expect(
      (["in_sync", "drift", "error", "pending", "off"] as const).map((s) => stateTone(s)),
    ).toEqual(["healthy", "degraded", "down", "pending", "neutral"]);
  });

  it("lists wide IPs with their state and syncs", async () => {
    renderIt();
    expect(await screen.findByText("shop.gslb.example.com")).toBeInTheDocument();
    expect(await screen.findByText("Drift")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Sync now" }));
    expect(await screen.findByText("3 records written, 1 failed")).toBeInTheDocument();
  });

  it("creates a latency wide IP with a virtual service member and an HTTP check", async () => {
    renderIt();
    await waitFor(() => expect(screen.getByRole("button", { name: "New wide IP" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "New wide IP" }));
    await userEvent.type(await screen.findByLabelText("Name"), "api");
    await userEvent.click(screen.getByLabelText("Lowest latency"));
    await userEvent.selectOptions(screen.getByLabelText("Check"), "http");
    await userEvent.type(screen.getByLabelText("Check path"), "/healthz");
    await pick(screen.getByRole("combobox", { name: "Member 1 virtual service" }), "shop");
    await userEvent.type(screen.getByLabelText("Member 1 site"), "pve1");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(calls[0]?.what).toBe("create"));
    expect(calls[0]?.body).toMatchObject({
      name: "api",
      algorithm: "latency",
      health: { kind: "http", path: "/healthz" },
      members: [{ virtual_service: "shop", address: null, site: "pve1" }],
    });
  });

  it("edits a weighted wide IP, then deletes after confirming", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("button", { name: "shop.gslb.example.com" }));
    expect(screen.queryByLabelText("Member 1 weight")).toBeNull();
    await userEvent.click(screen.getByLabelText("Weighted"));
    await userEvent.clear(screen.getByLabelText("Member 1 weight"));
    await userEvent.type(screen.getByLabelText("Member 1 weight"), "5");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls[0]?.what).toBe("patch"));
    expect(calls[0]?.body).toMatchObject({ algorithm: "weighted", members: [{ weight: 5 }] });
    await userEvent.click(await screen.findByRole("button", { name: "shop.gslb.example.com" }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(calls.some((c) => c.what === "delete")).toBe(true));
  });

  it("enables DNSSEC and shows the DS records", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("button", { name: "Enable DNSSEC" }));
    expect(await screen.findByLabelText("DS records")).toHaveTextContent("1234 13 2 abcd");
  });

  it("lets only a system administrator add a zone", async () => {
    renderIt();
    expect(await screen.findByRole("button", { name: "Add zone" })).toBeDisabled();
  });

  it("adds a zone as a system administrator", async () => {
    isSystem = true;
    renderIt();
    await waitFor(() => expect(screen.getByRole("button", { name: "Add zone" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Add zone" }));
    await userEvent.type(await screen.findByLabelText("Zone name"), "g2.example.com");
    await userEvent.type(
      screen.getByRole("combobox", { name: "Nameservers" }),
      "ns1.example.com{Enter}",
    );
    await userEvent.click(screen.getAllByRole("button", { name: "Add zone" }).at(-1)!);
    await waitFor(() => expect(calls[0]?.what).toBe("zone"));
    expect(calls[0]?.body).toMatchObject({
      name: "g2.example.com",
      nameservers: ["ns1.example.com"],
    });
  });

  it("takes client subnets for the subnet algorithm and removes a member", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("button", { name: "shop.gslb.example.com" }));
    await userEvent.click(screen.getByLabelText("By client subnet"));
    await userEvent.type(
      screen.getByRole("combobox", { name: "Member 1 subnets" }),
      "10.0.0.0/8,bogus,",
    );
    expect(await screen.findByText("Not a valid network")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add member" }));
    await userEvent.type(screen.getByLabelText("Member 2 address"), "198.51.100.20");
    await userEvent.click(screen.getByRole("button", { name: "Remove member 2" }));
    expect(screen.queryByLabelText("Member 2 address")).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("Check"), "http");
    await userEvent.selectOptions(screen.getByLabelText("Check scheme"), "http");
    await userEvent.type(screen.getByLabelText("Check match"), "ok");
    await userEvent.selectOptions(screen.getByLabelText("Check"), "tcp");
    await userEvent.click(screen.getByLabelText("Enabled"));
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(calls[0]?.what).toBe("patch"));
    expect(calls[0]?.body).toMatchObject({ algorithm: "subnet", enabled: false });
  });

  it("shows a server finding on the name", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/gslb-zones/gslb.example.com/wide-ips", () =>
        HttpResponse.json(
          {
            type: "about:blank",
            title: "Invalid",
            status: 422,
            errors: [{ path: "name", message: "Not a DNS label" }],
          },
          { status: 422, headers: { "content-type": "application/problem+json" } },
        ),
      ),
    );
    renderIt();
    await waitFor(() => expect(screen.getByRole("button", { name: "New wide IP" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "New wide IP" }));
    await userEvent.type(await screen.findByLabelText("Name"), "Bad_Name");
    await userEvent.type(screen.getByLabelText("Member 1 address"), "2001:db8::1");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("Not a DNS label")).toBeInTheDocument();
  });
});
