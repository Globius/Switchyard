// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { addressLine } from "@/features/segroups/SeGroups";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const GROUP = {
  id: "g1",
  tenant_id: "t1",
  name: "se-edge-a",
  ha_mode: "bgp",
  maxconn: 50000,
  canary_seconds: 30,
  routing_profile_id: null,
  networks: [{ network_id: "n1", role: "vip" }],
  version: 3,
  draft_status: "unchanged",
  changed_fields: [],
};
const ENGINE = (id: string, name: string, state = "enabled") => ({
  id,
  name,
  url: "http://x",
  state,
  se_group_id: "g1",
  agent_enrolled: true,
  mgmt_ip: "10.40.0.21",
  vmid: 100,
  pve_node: "pve-atl-01",
  guest_kind: "qemu",
});
const STATUS = (id: string, name: string, drift: unknown[] = []) => ({
  engine: name,
  engine_id: id,
  se_group_id: "g1",
  reachable: true,
  members: [],
  drift,
  observed_at: "2026-10-11T10:52:14Z",
  agent: {
    version: "0.9.2",
    waf_expected: id === "e2",
    waf_running: false,
    bgp: [{ peer: "10.40.12.2", remote_asn: 65000, state: "Established" }],
  },
});

let lxcEnabled = true;
let stateCall: { engine: string; state: string | null } | null = null;
let patched: { body: unknown; id: string } | null = null;
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
      features: { lxc_guests: lxcEnabled },
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "write", object: "application", min_role: "operator", description: "Edit." },
        { verb: "manage", object: "engine", min_role: "tenant-admin", description: "Engines." },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/se-groups", () =>
    HttpResponse.json({ items: [GROUP], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/engines", () =>
    HttpResponse.json([ENGINE("e1", "se-edge-a-01"), ENGINE("e2", "se-edge-a-02")]),
  ),
  http.get("*/api/v1/tenants/acme/status/engines", () =>
    HttpResponse.json([
      STATUS("e1", "se-edge-a-01"),
      STATUS("e2", "se-edge-a-02", [{ kind: "file_hash", path: "haproxy.cfg" }]),
    ]),
  ),
  http.get("*/api/v1/tenants/acme/routing-profiles", () =>
    HttpResponse.json({ items: [{ id: "r1", name: "edge-bgp" }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/networks", () =>
    HttpResponse.json({
      items: [{ id: "n1", name: "edge-vip", cidr: "10.40.12.0/24" }],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/tenants/acme/vip-pools", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.put("*/api/v1/tenants/acme/engines/:id/state", ({ params, request }) => {
    stateCall = {
      engine: String(params.id),
      state: new URL(request.url).searchParams.get("state"),
    };
    return HttpResponse.json({ state: "disabled" });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  stateCall = null;
  patched = null;
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

describe("SE groups", () => {
  it("shows engines with live state, BGP sessions, drift, and disables one immediately", async () => {
    renderAt("/t/acme/se-groups");
    expect(await screen.findByText("se-edge-a-01")).toBeInTheDocument();
    expect(await screen.findByText("In sync")).toBeInTheDocument();
    const row = screen.getByText("se-edge-a-02").closest("tr")!;
    expect(await within(row).findByText("Drifted")).toBeInTheDocument();
    expect(within(row).getByText(/10\.40\.12\.2 Established/)).toBeInTheDocument();
    expect(within(row).getByText(/agent 0.9.2/)).toBeInTheDocument();
    expect(within(row).getByText("WAF sidecar not running")).toBeInTheDocument();
    const disable = within(row).getByRole("button", { name: "Disable" });
    await waitFor(() => expect(disable).toBeEnabled());
    await userEvent.click(disable);
    await waitFor(() => expect(stateCall).toEqual({ engine: "e2", state: "disabled" }));
  });

  it("saves only the changed settings to the draft", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/change-sets", () =>
        HttpResponse.json(
          {
            id: "cs1",
            state: "draft",
            summary: "Draft by Miguel, 10:00",
            author: "Miguel",
            base_revision: 1,
            created_at: "2026-10-11T10:00:00Z",
            op_count: 0,
          },
          { status: 201 },
        ),
      ),
      http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", async ({ request }) => {
        const b = (await request.json()) as { ops: { object_id: string; body: unknown }[] };
        patched = { id: b.ops[0]!.object_id, body: b.ops[0]!.body };
        return HttpResponse.json({ ok: true });
      }),
    );
    renderAt("/t/acme/se-groups");
    const max = await screen.findByLabelText("Max connections");
    await waitFor(() => expect(max).toBeEnabled());
    await userEvent.clear(max);
    await userEvent.type(max, "80000");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(patched).toEqual({ id: "g1", body: { maxconn: 80000 } }));
  });

  it("deploys VMs with the chosen nodes and management pool", async () => {
    let body: unknown = null;
    server.use(
      http.get("*/api/v1/tenants/acme/vip-pools", () =>
        HttpResponse.json({ items: [{ id: "mp", name: "se-mgmt" }], next_cursor: null }),
      ),
      http.post("*/api/v1/tenants/acme/se-groups/g1/deploy", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ job_id: "j" }, { status: 202 });
      }),
    );
    renderAt("/t/acme/se-groups");
    const open = await screen.findByRole("button", { name: "Deploy engines" });
    await waitFor(() => expect(open).toBeEnabled());
    await userEvent.click(open);
    await userEvent.type(await screen.findByLabelText("Template VM ID"), "9001");
    await userEvent.type(screen.getByLabelText("Proxmox nodes"), "pve-01,");
    await userEvent.selectOptions(
      await screen.findByLabelText("Management addresses from"),
      await screen.findByRole("option", { name: "se-mgmt" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Start deploy" }));
    await waitFor(() =>
      expect(body).toMatchObject({
        count: 1,
        template_vmid: 9001,
        nodes: ["pve-01"],
        mgmt_pool_id: "mp",
      }),
    );
    expect(await screen.findByText(/Deploying as a job/)).toBeInTheDocument();
  });

  it("deploys containers only after the risks are confirmed", async () => {
    let body: unknown = null;
    server.use(
      http.get("*/api/v1/tenants/acme/vip-pools", () =>
        HttpResponse.json({ items: [{ id: "mp", name: "se-mgmt" }], next_cursor: null }),
      ),
      http.post("*/api/v1/tenants/acme/se-groups/g1/deploy", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ job_id: "j" }, { status: 202 });
      }),
    );
    renderAt("/t/acme/se-groups");
    const open = await screen.findByRole("button", { name: "Deploy engines" });
    await waitFor(() => expect(open).toBeEnabled());
    await userEvent.click(open);
    const dlg = within(await screen.findByRole("dialog"));
    expect(dlg.getByRole("radio", { name: /Virtual machine/ })).toBeChecked();
    expect(dlg.queryByText("Containers are privileged and partly untested")).toBeNull();
    await userEvent.click(dlg.getByRole("radio", { name: /Container \(LXC\)/ }));
    expect(dlg.getByText("Containers are privileged and partly untested")).toBeInTheDocument();
    await userEvent.type(dlg.getByLabelText("Template container ID"), "9100");
    await userEvent.type(dlg.getByLabelText("Proxmox nodes"), "pve-01,");
    await userEvent.selectOptions(
      await dlg.findByLabelText("Management addresses from"),
      await dlg.findByRole("option", { name: "se-mgmt" }),
    );
    const start = dlg.getByRole("button", { name: "Start deploy" });
    expect(start).toBeDisabled();
    expect(dlg.getByText("Confirm the container risks above to deploy.")).toBeInTheDocument();
    await userEvent.click(dlg.getByRole("checkbox"));
    expect(start).toBeEnabled();
    await userEvent.click(dlg.getByRole("radio", { name: /Virtual machine/ }));
    await userEvent.click(dlg.getByRole("radio", { name: /Container \(LXC\)/ }));
    expect(dlg.getByRole("checkbox")).not.toBeChecked();
    await userEvent.click(dlg.getByRole("checkbox"));
    await userEvent.click(start);
    await waitFor(() =>
      expect(body).toMatchObject({ guest_kind: "lxc", template_vmid: 9100, nodes: ["pve-01"] }),
    );
  });

  it("shows each engine's guest type, Proxmox ID and node", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/engines", () =>
        HttpResponse.json([
          { ...ENGINE("e1", "se-edge-a-01"), vmid: 201 },
          { ...ENGINE("e3", "se-edge-a-03"), vmid: 203, guest_kind: "lxc" },
          { ...ENGINE("e4", "se-edge-a-04"), guest_kind: null, vmid: null, pve_node: null },
        ]),
      ),
    );
    renderAt("/t/acme/se-groups");
    expect(await screen.findByText(/^VM 201 · pve-atl-01 · 10\.40\.0\.21/)).toBeInTheDocument();
    expect(screen.getByText(/^CT 203 · pve-atl-01 · 10\.40\.0\.21/)).toBeInTheDocument();
    expect(screen.getByText(/^10\.40\.0\.21 · agent|^10\.40\.0\.21$/)).toBeInTheDocument();
  });

  async function openDeploy() {
    renderAt("/t/acme/se-groups");
    const open = await screen.findByRole("button", { name: "Deploy engines" });
    await waitFor(() => expect(open).toBeEnabled());
    await userEvent.click(open);
    return within(await screen.findByRole("dialog"));
  }
  async function fillDeploy(dlg: ReturnType<typeof within>) {
    await userEvent.type(dlg.getByLabelText("Template VM ID"), "9001");
    await userEvent.type(dlg.getByLabelText("Proxmox nodes"), "pve-01,");
    await userEvent.selectOptions(await dlg.findByLabelText("Management addresses from"), "mp");
  }
  const POOLS = (items: unknown[]) =>
    http.get("*/api/v1/tenants/acme/vip-pools", () =>
      HttpResponse.json({ items, next_cursor: null }),
    );

  it("preselects the only pool on a network and sends it as nic_pools", async () => {
    let body: unknown = null;
    server.use(
      POOLS([
        { id: "mp", name: "se-mgmt", network_id: "nm" },
        { id: "vp", name: "vip-edge", network_id: "n1" },
      ]),
      http.get("*/api/v1/tenants/acme/vip-pools/:id/allocations", () =>
        HttpResponse.json({
          allocations: [],
          free: 58,
          total: 100,
          used: 42,
          next_free: null,
          pool_id: "vp",
        }),
      ),
      http.post("*/api/v1/tenants/acme/se-groups/g1/deploy", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ job_id: "j" }, { status: 202 });
      }),
    );
    const dlg = await openDeploy();
    const picker = await dlg.findByRole("combobox", { name: "Addresses on edge-vip" });
    await waitFor(() => expect(picker).toHaveTextContent("vip-edge · 58 free"));
    expect(dlg.queryByText(/can't serve VIPs on edge-vip/)).toBeNull();
    await fillDeploy(dlg);
    await userEvent.click(dlg.getByRole("button", { name: "Start deploy" }));
    await waitFor(() => expect(body).toMatchObject({ nic_pools: { n1: "vp" } }));
  });

  it("warns on an unaddressed NIC, and omits it from the request", async () => {
    let body: Record<string, unknown> | null = null;
    server.use(
      POOLS([{ id: "mp", name: "se-mgmt", network_id: "nm" }]),
      http.post("*/api/v1/tenants/acme/se-groups/g1/deploy", async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({ job_id: "j" }, { status: 202 });
      }),
    );
    const dlg = await openDeploy();
    expect(await dlg.findByText(/can't serve VIPs on edge-vip until someone/)).toBeInTheDocument();
    expect(dlg.getByText("No pools on this network.")).toBeInTheDocument();
    expect(dlg.getByRole("combobox", { name: "Addresses on edge-vip" })).toBeDisabled();
    await fillDeploy(dlg);
    await userEvent.click(dlg.getByRole("button", { name: "Start deploy" }));
    await waitFor(() => expect(body).not.toBeNull());
    expect(body).not.toHaveProperty("nic_pools");
  });

  it("puts a pool-capacity error on the network's row", async () => {
    server.use(
      POOLS([
        { id: "mp", name: "se-mgmt", network_id: "nm" },
        { id: "vp", name: "vip-edge", network_id: "n1" },
      ]),
      http.post("*/api/v1/tenants/acme/se-groups/g1/deploy", () =>
        HttpResponse.json(
          {
            title: "Unprocessable",
            status: 422,
            errors: [
              { path: "nic_pools.n1", code: "pool_exhausted", message: "Only 1 free address" },
            ],
          },
          { status: 422 },
        ),
      ),
    );
    const dlg = await openDeploy();
    await fillDeploy(dlg);
    await userEvent.click(dlg.getByRole("button", { name: "Start deploy" }));
    expect(await dlg.findByText("Only 1 free address")).toBeInTheDocument();
    expect(dlg.queryByText("Unprocessable")).toBeNull();
  });

  it("shows each engine's address per VIP and backend network", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/engines", () =>
        HttpResponse.json([
          {
            ...ENGINE("e1", "se-edge-a-01"),
            addresses: [{ network_id: "n1", address: "10.40.12.6" }],
          },
          { ...ENGINE("e2", "se-edge-a-02"), addresses: [] },
        ]),
      ),
    );
    renderAt("/t/acme/se-groups");
    expect(await screen.findByText("edge-vip 10.40.12.6")).toBeInTheDocument();
    expect(screen.queryByText(/edge-vip —/)).toBeNull();
  });

  it("marks unaddressed networks with a dash and skips management", () => {
    const nets = [
      { network_id: "a", role: "vip" },
      { network_id: "b", role: "backend" },
      { network_id: "m", role: "mgmt" },
    ];
    const name = (id: string) => ({ a: "edge-vip", b: "backend", m: "se-mgmt" })[id] ?? id;
    expect(
      addressLine({ addresses: [{ network_id: "a", address: "10.40.12.6" }] }, nets, name),
    ).toBe("edge-vip 10.40.12.6 · backend —");
    expect(addressLine({ addresses: [] }, nets, name)).toBe("");
    expect(addressLine({}, nets, name)).toBe("");
  });

  it("hides the container option while LXC guests are off, and explains a lxc_disabled refusal", async () => {
    lxcEnabled = false;
    server.use(
      http.get("*/api/v1/tenants/acme/vip-pools", () =>
        HttpResponse.json({
          items: [{ id: "mp", name: "se-mgmt", network_id: "nm" }],
          next_cursor: null,
        }),
      ),
      http.post("*/api/v1/tenants/acme/se-groups/g1/deploy", async ({ request }) => {
        expect(await request.json()).toMatchObject({ guest_kind: "qemu" });
        return HttpResponse.json(
          { title: "Forbidden", status: 403, code: "lxc_disabled", detail: "off" },
          { status: 403 },
        );
      }),
    );
    try {
      renderAt("/t/acme/se-groups");
      const open = await screen.findByRole("button", { name: "Deploy engines" });
      await waitFor(() => expect(open).toBeEnabled());
      await userEvent.click(open);
      const dlg = within(await screen.findByRole("dialog"));
      expect(dlg.queryByRole("radio", { name: /Container/ })).toBeNull();
      await userEvent.type(dlg.getByLabelText("Template VM ID"), "9001");
      await userEvent.type(dlg.getByLabelText("Proxmox nodes"), "pve-01,");
      await userEvent.selectOptions(await dlg.findByLabelText("Management addresses from"), "mp");
      await userEvent.click(dlg.getByRole("button", { name: "Start deploy" }));
      expect(
        await dlg.findByText(/Containers are turned off on this controller/),
      ).toBeInTheDocument();
    } finally {
      lxcEnabled = true;
    }
  });

  it("shares persistence tables between engines and sends the peers", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/change-sets", () =>
        HttpResponse.json({
          items: [
            {
              id: "cs1",
              state: "draft",
              author: "Miguel",
              summary: "d",
              base_revision: 1,
              created_at: "2026-10-11T10:00:00Z",
              op_count: 0,
            },
          ],
          next_cursor: null,
        }),
      ),
      http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", async ({ request }) => {
        const b = (await request.json()) as { ops: { object_id: string; body: unknown }[] };
        patched = { id: b.ops[0]!.object_id, body: b.ops[0]!.body };
        return HttpResponse.json({ ok: true });
      }),
    );
    renderAt("/t/acme/se-groups");
    const sw = await screen.findByRole("switch", { name: "Share persistence tables" });
    await waitFor(() => expect(sw).toBeEnabled());
    await userEvent.click(sw);
    expect(screen.getByLabelText("Peers port")).toHaveValue(10000);
    await userEvent.type(screen.getByLabelText("Peer 1 address"), "10.20.0.5");
    await userEvent.type(screen.getByLabelText("Peer 2 address"), "2001:db8:20::6");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() =>
      expect(patched).toMatchObject({
        body: {
          peers: {
            port: 10000,
            members: [{ address: "10.20.0.5" }, { address: "2001:db8:20::6" }],
          },
        },
      }),
    );
  });

  it("registers an existing VM", async () => {
    let body: unknown = null;
    server.use(
      http.post("*/api/v1/tenants/acme/se-groups/g1/engines", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(ENGINE("e3", "new"), { status: 201 });
      }),
    );
    renderAt("/t/acme/se-groups");
    const open = await screen.findByRole("button", { name: "Register existing engine" });
    await waitFor(() => expect(open).toBeEnabled());
    await userEvent.click(open);
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Name"), "se-edge-a-03");
    await userEvent.type(dlg.getByLabelText("Data Plane API URL"), "http://10.0.0.3:5555");
    await userEvent.click(dlg.getByRole("button", { name: "Register" }));
    await waitFor(() =>
      expect(body).toMatchObject({ name: "se-edge-a-03", dataplane_url: "http://10.0.0.3:5555" }),
    );
  });

  it("creates an enrollment token and removes an engine record after confirming", async () => {
    let removed = false;
    server.use(
      http.post("*/api/v1/tenants/acme/engines/e1/enrollment-token", () =>
        HttpResponse.json({ token: "tok-123", expires_at: "2026-10-12T10:00:00Z" }),
      ),
      http.delete("*/api/v1/tenants/acme/engines/e2", () => {
        removed = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderAt("/t/acme/se-groups");
    const row1 = (await screen.findByText("se-edge-a-01")).closest("tr")!;
    const more1 = within(row1).getByRole("button", { name: "More" });
    await waitFor(() => expect(more1).toBeEnabled());
    await userEvent.click(more1);
    await userEvent.click(await screen.findByRole("menuitem", { name: "Enrollment token" }));
    expect(await screen.findByText("tok-123")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("I have stored it"));
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    const row2 = screen.getByText("se-edge-a-02").closest("tr")!;
    await userEvent.click(within(row2).getByRole("button", { name: "More" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Remove record" }));
    await userEvent.click(await screen.findByRole("button", { name: "Remove record" }));
    await waitFor(() => expect(removed).toBe(true));
  });

  it("adds a new SE group to the draft", async () => {
    server.use(
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
        const b = (await request.json()) as { ops: { body: unknown }[] };
        patched = { id: "new", body: b.ops[0]!.body };
        return HttpResponse.json({ ok: true });
      }),
    );
    renderAt("/t/acme/se-groups");
    const open = await screen.findByRole("button", { name: /New SE group/ });
    await waitFor(() => expect(open).toBeEnabled());
    await userEvent.click(open);
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Name"), "se-core");
    await userEvent.selectOptions(dlg.getByLabelText("Routing mode"), "vrrp");
    await userEvent.click(dlg.getByRole("button", { name: "Add to draft" }));
    await waitFor(() =>
      expect(patched).toEqual({
        id: "new",
        body: { name: "se-core", ha_mode: "vrrp", networks: [] },
      }),
    );
  });

  it("shows where engines run on Proxmox and warns when one host carries them all", async () => {
    const node = (name: string, engines: object[]) => ({
      name,
      online: true,
      cpu: 0.25,
      max_cpu: 16,
      mem_used: 8 * 1024 ** 3,
      mem_total: 64 * 1024 ** 3,
      uptime_s: 1000,
      storage: [],
      engines,
    });
    server.use(
      http.get("*/api/v1/tenants/acme/se-groups/g1/placement", () =>
        HttpResponse.json({
          available: true,
          reason: null,
          nodes: [
            node("pve-01", [
              { id: "e1", name: "se-edge-a-01", state: "enabled", vmid: 101 },
              { id: "e2", name: "se-edge-a-02", state: "enabled", vmid: 102 },
            ]),
            node("pve-02", []),
          ],
        }),
      ),
    );
    renderAt("/t/acme/se-groups");
    expect(await screen.findByText("Placement on Proxmox")).toBeInTheDocument();
    expect(await screen.findByText(/se-edge-a-01 · VM 101/)).toBeInTheDocument();
    expect(screen.getAllByText("CPU 25% of 16 · memory 8 of 64 GiB")).toHaveLength(2);
    expect(screen.getByText(/All engines run on pve-01/)).toBeInTheDocument();
    expect(screen.getByText("No engines of yours")).toBeInTheDocument();
  });

  it("says host state is not reported with the reason", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/se-groups/g1/placement", () =>
        HttpResponse.json({ available: false, reason: "Proxmox is not configured.", nodes: [] }),
      ),
    );
    renderAt("/t/acme/se-groups");
    expect(
      await screen.findByText("Host state not reported: Proxmox is not configured."),
    ).toBeInTheDocument();
  });

  it("shows an engine's running config next to the desired one, with hashes for agent files", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/engines/e2/running-config", () =>
        HttpResponse.json({
          engine: "se-edge-a-02",
          engine_id: "e2",
          se_group_id: "g1",
          reachable: true,
          error: null,
          fetched_at: "2026-10-11T10:00:00Z",
          haproxy: {
            content: "global\n    maxconn 100\n",
            fingerprint: "1111111111111111",
            desired_fingerprint: "2222222222222222",
            fingerprint_matches: false,
            matches_desired: false,
          },
          agent: {
            bundle_id: "b1b1b1b1b1b1b1",
            desired_bundle_id: "b2b2b2b2b2b2b2",
            files: [
              {
                path: "/etc/switchyard/maps/web.map",
                sha256: "aaaaaaaaaaaaaaaa",
                desired_sha256: "bbbbbbbbbbbbbbbb",
                matches_desired: false,
              },
            ],
          },
        }),
      ),
    );
    renderAt("/t/acme/se-groups");
    await userEvent.click(await screen.findByRole("button", { name: "View running config" }));
    const dlg = within(await screen.findByRole("dialog"));
    expect((await dlg.findAllByText("Differs")).length).toBe(2);
    expect(dlg.getByText(/maxconn 100/)).toBeInTheDocument();
    expect(dlg.getByText("/etc/switchyard/maps/web.map")).toBeInTheDocument();
    expect(
      dlg.getByText("The agent reports hashes only, never file contents."),
    ).toBeInTheDocument();
  });
});
