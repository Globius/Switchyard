import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { profileLine } from "@/features/routing/RoutingProfiles";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const BASE = { tenant_id: "t1", draft_status: "unchanged", changed_fields: [], version: 2 };
const BGP = {
  ...BASE,
  id: "r1",
  kind: "bgp",
  name: "edge-bgp",
  asn: 65040,
  max_paths: 4,
  max_prefixes: 5000,
  bfd: true,
  peers: [
    { peer_ip: "10.40.12.2", remote_asn: 65000, md5_ref: "openbao://kv/x" },
    { peer_ip: "10.40.12.3", remote_asn: 65000, md5_ref: null },
  ],
  static_routes: [{ prefix: "10.40.30.0/23", next_hop: "10.40.12.1" }],
};
const VRRP = {
  ...BASE,
  id: "r2",
  kind: "vrrp",
  name: "core-vrrp",
  vrid: 51,
  interface: "eth1",
  base_priority: 150,
  priority_step: 10,
  advert_interval_s: 1,
  static_routes: [],
};

const ops: { object_id: string; action: string; body?: Record<string, unknown> }[] = [];
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
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/routing-profiles", () =>
    HttpResponse.json({ items: [BGP, VRRP], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/se-groups", () =>
    HttpResponse.json({
      items: [{ id: "g1", name: "se-edge-a", routing_profile_id: "r1" }],
      next_cursor: null,
    }),
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
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  ops.length = 0;
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

describe("routing profiles", () => {
  it("describes a profile in one line", () => {
    expect(profileLine(BGP as never, ["se-edge-a"])).toBe("BGP · AS 65040 · 2 peers · se-edge-a");
    expect(profileLine(VRRP as never, [])).toBe("VRRP · VRID 51");
  });

  it("lists profiles and edits a BGP profile: peers and ASN go to the draft as one update", async () => {
    renderAt("/t/acme/routing-profiles");
    expect(await screen.findByText("BGP · AS 65040 · 2 peers · se-edge-a")).toBeInTheDocument();
    expect(screen.getByText("VRRP · VRID 51")).toBeInTheDocument();
    const asn = await screen.findByLabelText("Local ASN");
    await waitFor(() => expect(asn).toBeEnabled());
    await userEvent.clear(asn);
    await userEvent.type(asn, "65041");
    await userEvent.click(screen.getByRole("button", { name: "Remove peer 2" }));
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(ops).toHaveLength(1));
    expect(ops[0]).toMatchObject({ action: "update", object_id: "r1" });
    expect(ops[0]!.body).toEqual({
      asn: 65041,
      peers: [{ peer_ip: "10.40.12.2", remote_asn: 65000, md5_ref: "openbao://kv/x" }],
    });
    expect(screen.getByText("se-edge-a", { selector: "a" })).toBeInTheDocument();
  });

  it("edits BGP prefix limits on the profile and a peer", async () => {
    renderAt("/t/acme/routing-profiles");
    const prof = await screen.findByLabelText("Max prefixes");
    await waitFor(() => expect(prof).toBeEnabled());
    expect(prof).toHaveValue(5000);
    await userEvent.clear(prof);
    await userEvent.type(prof, "9000");
    await userEvent.type(screen.getByLabelText("Peer 2 max prefixes"), "100");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(ops).toHaveLength(1));
    expect(ops[0]!.body).toMatchObject({ max_prefixes: 9000 });
    expect((ops[0]!.body as { peers: { max_prefixes?: number }[] }).peers[1]!.max_prefixes).toBe(
      100,
    );
  });

  it("edits a VRRP profile and adds a static route", async () => {
    renderAt("/t/acme/routing-profiles");
    await userEvent.click(await screen.findByRole("button", { name: /core-vrrp/ }));
    const vrid = await screen.findByLabelText("VRID");
    await waitFor(() => expect(vrid).toBeEnabled());
    await userEvent.clear(vrid);
    await userEvent.type(vrid, "52");
    await userEvent.click(screen.getByRole("button", { name: "Add route" }));
    await userEvent.type(screen.getByLabelText("Prefix 1"), "10.1.0.0/16");
    await userEvent.type(screen.getByLabelText("Next hop 1"), "10.0.0.1");
    await userEvent.click(screen.getByRole("button", { name: "Save to draft" }));
    await waitFor(() => expect(ops).toHaveLength(1));
    expect(ops[0]!.body).toEqual({
      vrid: 52,
      static_routes: [{ prefix: "10.1.0.0/16", next_hop: "10.0.0.1" }],
    });
  });

  it("creates a BGP profile and deletes one after confirming", async () => {
    renderAt("/t/acme/routing-profiles");
    const open = await screen.findByRole("button", { name: "New routing profile" });
    await waitFor(() => expect(open).toBeEnabled());
    await userEvent.click(open);
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Name"), "lab-bgp");
    await userEvent.type(dlg.getByLabelText("Local ASN"), "65100");
    await userEvent.type(dlg.getByLabelText("First peer IP"), "10.9.0.1");
    await userEvent.type(dlg.getByLabelText("Remote ASN"), "65000");
    await userEvent.click(dlg.getByRole("button", { name: "Add to draft" }));
    await waitFor(() => expect(ops).toHaveLength(1));
    expect(ops[0]).toMatchObject({
      action: "create",
      body: { kind: "bgp", name: "lab-bgp", asn: 65100 },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await userEvent.click(await screen.findByRole("button", { name: "Add delete to draft" }));
    await waitFor(() => expect(ops).toHaveLength(2));
    expect(ops[1]).toMatchObject({ action: "delete", object_id: "r1" });
  });
});
