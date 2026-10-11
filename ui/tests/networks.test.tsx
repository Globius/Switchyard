import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { rangesText } from "@/features/networks/Networks";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const BASE = { tenant_id: "t1", draft_status: "unchanged", changed_fields: [], version: 2 };
const NETS = [
  {
    ...BASE,
    id: "n1",
    name: "edge-vip",
    cidr: "10.40.12.0/24",
    gateway: "10.40.12.1",
    sdn_zone: "evpn-prod",
    vnet: "vnedge",
    vlan_or_vni: 10012,
    managed: true,
  },
  {
    ...BASE,
    id: "n2",
    name: "backend",
    cidr: "10.40.30.0/23",
    gateway: null,
    sdn_zone: null,
    vnet: null,
    vlan_or_vni: null,
    managed: false,
  },
];
const POOL = {
  ...BASE,
  id: "p1",
  name: "vip-edge",
  network_id: "n1",
  ranges: [{ start: "10.40.12.10", end: "10.40.12.71" }],
};
const usage = (allocs: unknown[]) => ({
  pool_id: "p1",
  total: 62,
  used: allocs.length,
  free: 62 - allocs.length,
  next_free: null,
  allocations: allocs,
});
const A = (address: string, name: string, enabled = true) => ({
  address,
  draft_status: "unchanged",
  enabled,
  virtual_service_id: `v-${name}`,
  virtual_service_name: name,
});

const ops: { object_type: string; action: string; body?: Record<string, unknown> }[] = [];
let sdnCalled = false;
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
      tenants: [{ id: "t1", name: "acme", role: "tenant-admin", via: [] }],
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "write", object: "application", min_role: "operator", description: "Edit." },
        { verb: "apply", object: "sdn", min_role: "tenant-admin", description: "SDN." },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/networks", () =>
    HttpResponse.json({ items: NETS, next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/vip-pools", () =>
    HttpResponse.json({ items: [POOL], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/se-groups", () =>
    HttpResponse.json({
      items: [{ id: "g1", name: "se-edge-a", networks: [{ network_id: "n1", role: "vip" }] }],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/tenants/acme/vip-pools/p1/allocations", () =>
    HttpResponse.json(usage([A("10.40.12.10", "web-portal"), A("10.40.12.14", "grafana", false)])),
  ),
  http.post("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json(
      {
        id: "cs1",
        state: "draft",
        summary: "d",
        author: "Miguel",
        base_revision: 1,
        created_at: "2026-10-11T10:00:00Z",
        op_count: 0,
      },
      { status: 201 },
    ),
  ),
  http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", async ({ request }) => {
    const b = (await request.json()) as { ops: (typeof ops)[number][] };
    ops.push(...b.ops);
    return HttpResponse.json({ ok: true });
  }),
  http.get("*/api/v1/tenants/acme/sdn/status", () =>
    HttpResponse.json([
      {
        network_id: "n1",
        network: "edge-vip",
        state: "running",
        job_id: "j",
        started_at: null,
        finished_at: null,
        error: null,
      },
      {
        network_id: "n2",
        network: "backend",
        state: "failed",
        job_id: "k",
        started_at: null,
        finished_at: null,
        error: "zone missing",
      },
    ]),
  ),
  http.post("*/api/v1/tenants/acme/networks/apply-sdn", () => {
    sdnCalled = true;
    return HttpResponse.json({ job_id: "j" }, { status: 202 });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  ops.length = 0;
  sdnCalled = false;
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
}

describe("networks and VIP pools", () => {
  it("formats ranges", () => {
    expect(rangesText(POOL)).toBe("10.40.12.10 – 10.40.12.71");
  });

  it("lists networks with ownership and who uses them, pools with usage, and the addresses", async () => {
    renderAt("/t/acme/networks");
    const row = (await screen.findByRole("button", { name: "edge-vip" })).closest("tr")!;
    expect(within(row).getByText("Created by Switchyard")).toBeInTheDocument();
    expect(await within(row).findByText("SDN apply running")).toBeInTheDocument();
    expect(await screen.findByText("SDN apply failed")).toBeInTheDocument();
    expect(within(row).getByText("evpn-prod · vnedge")).toBeInTheDocument();
    expect(within(row).getByText("VNI 10012")).toBeInTheDocument();
    expect(within(row).getByText("se-edge-a (VIP), vip-edge")).toBeInTheDocument();
    const back = screen.getByRole("button", { name: "backend" }).closest("tr")!;
    expect(within(back).getByText("Attached")).toBeInTheDocument();
    expect(await screen.findByText("2 of 62 used")).toBeInTheDocument();
    expect(await screen.findByText("10.40.12.10")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "web-portal" })).toHaveAttribute(
      "href",
      "/t/acme/virtual-services/v-web-portal",
    );
    expect(screen.getByText("Live · held")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Edit service" })).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Find IP or service"), "grafana");
    expect(screen.queryByText("web-portal")).not.toBeInTheDocument();
  });

  it("marks addresses freed or added by the draft", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/change-sets", () =>
        HttpResponse.json({
          items: [
            {
              id: "cs1",
              state: "draft",
              summary: "d",
              author: "Miguel",
              base_revision: 1,
              created_at: "2026-10-11T10:00:00Z",
              op_count: 1,
            },
          ],
          next_cursor: null,
        }),
      ),
      http.get("*/api/v1/tenants/acme/vip-pools/p1/allocations", ({ request }) =>
        HttpResponse.json(
          new URL(request.url).searchParams.has("change_set")
            ? usage([A("10.40.12.31", "docs-site")])
            : usage([A("10.40.12.30", "legacy-sso")]),
        ),
      ),
    );
    renderAt("/t/acme/networks");
    expect(await screen.findByText("Freed by draft")).toBeInTheDocument();
    expect(screen.getByText("In a draft · not reserved")).toBeInTheDocument();
    expect(screen.getByText(/1 of 62 used/)).toBeInTheDocument();
  });

  it("creates a network into the draft and starts an SDN apply", async () => {
    renderAt("/t/acme/networks");
    const create = await screen.findByRole("button", { name: "Create network" });
    await waitFor(() => expect(create).toBeEnabled());
    await userEvent.click(create);
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Name"), "lab-vip");
    await userEvent.type(dlg.getByLabelText("CIDR"), "10.40.40.0/24");
    await userEvent.type(dlg.getByLabelText("VLAN / VNI"), "10040");
    await userEvent.click(dlg.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(ops).toHaveLength(1));
    expect(ops[0]).toMatchObject({
      object_type: "network",
      action: "create",
      body: { name: "lab-vip", cidr: "10.40.40.0/24", vlan_or_vni: 10040, managed: true },
    });
    const apply = await screen.findByRole("button", { name: "Apply SDN" });
    await waitFor(() => expect(apply).toBeEnabled());
    await userEvent.click(apply);
    await waitFor(() => expect(sdnCalled).toBe(true));
    expect(await screen.findByText(/SDN apply started/)).toBeInTheDocument();
  });

  it("edits a network sending only the changed field, and edits pool ranges", async () => {
    renderAt("/t/acme/networks");
    await userEvent.click(await screen.findByRole("button", { name: "backend" }));
    const gw = await screen.findByLabelText("Gateway");
    await userEvent.type(gw, "10.40.30.1");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(ops).toHaveLength(1));
    expect(ops[0]).toMatchObject({ action: "update", body: { gateway: "10.40.30.1" } });
    await userEvent.click(await screen.findByRole("button", { name: "Edit ranges" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.click(dlg.getByRole("button", { name: "Add range" }));
    await userEvent.type(dlg.getByLabelText("Range 2 start"), "10.40.12.100");
    await userEvent.type(dlg.getByLabelText("Range 2 end"), "10.40.12.120");
    await userEvent.click(dlg.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(ops).toHaveLength(2));
    expect(ops[1]).toMatchObject({
      object_type: "vip_pool",
      action: "update",
      body: {
        ranges: [
          { start: "10.40.12.10", end: "10.40.12.71" },
          { start: "10.40.12.100", end: "10.40.12.120" },
        ],
      },
    });
  });
});
