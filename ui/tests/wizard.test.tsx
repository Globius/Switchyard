// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { QueryClientProvider } from "@tanstack/react-query";
import { pick } from "./pick";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import {
  buildOps,
  downAfterSeconds,
  initialState,
  stepTitles,
  validateStep,
  withProtocol,
  type WizardState,
} from "@/features/wizard/model";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

let n = 0;
const id = () => `id-${++n}`;
const filled = (o: Partial<WizardState> = {}): WizardState => ({
  ...initialState(),
  name: "docs-site",
  se_group_id: "g1",
  vip_pool_id: "vp1",
  vip: "192.0.2.50",
  new_pool_name: "docs-web",
  members: [{ address: "10.1.0.1", port: "8080", weight: 1 }],
  monitor: { ...initialState().monitor, name: "docs-check" },
  ...o,
});

describe("wizard model", () => {
  it("accepts IPv6 VIPs and members, and rejects loose text containing a colon", () => {
    const ok = filled({
      vipMode: "fixed",
      vip: "2001:db8:12::6",
      members: [{ address: "2001:db8:30::5", port: "8080", weight: 1 }],
    });
    expect(validateStep(2, ok, new Set())).toEqual({});
    expect(validateStep(4, ok, new Set())).toEqual({});
    expect(validateStep(2, filled({ vipMode: "fixed", vip: "a:b" }), new Set()).vip).toBeDefined();
    expect(
      validateStep(
        4,
        filled({ members: [{ address: "not:an:ip", port: "80", weight: 1 }] }),
        new Set(),
      ),
    ).toHaveProperty("members.0.address");
  });

  it("validates basics", () => {
    expect(validateStep(1, initialState(), new Set())).toMatchObject({
      name: expect.any(String),
      se_group_id: expect.any(String),
    });
    expect(validateStep(1, filled({ name: "Bad Name" }), new Set()).name).toBeDefined();
    expect(validateStep(1, filled(), new Set(["docs-site"])).name).toMatch(/already exists/);
    expect(validateStep(1, filled(), new Set())).toEqual({});
  });
  it("validates the VIP and ports", () => {
    expect(
      validateStep(2, filled({ vipMode: "pool", vip_pool_id: "" }), new Set()).vip_pool_id,
    ).toBeDefined();
    expect(validateStep(2, filled({ vipMode: "fixed", vip: "nope" }), new Set()).vip).toBeDefined();
    const ports = [
      { ...initialState().ports[0]!, port: 70000 },
      { ...initialState().ports[0]!, port: 80 },
      { ...initialState().ports[0]!, port: 80 },
    ];
    const e = validateStep(3, filled({ ports, certificate_ids: ["c"] }), new Set());
    expect(e["ports.0.port"]).toMatch(/1–65535/);
    expect(e["ports.2.port"]).toMatch(/already listed/);
    expect(validateStep(3, filled(), new Set()).certificate_ids).toBeDefined();
  });
  it("validates routes, pool members and the monitor", () => {
    const routes = [
      {
        host: "",
        kind: "prefix" as const,
        path: "",
        action: "pool" as const,
        redirect_location: "",
        redirect_code: 301 as const,
      },
    ];
    expect(validateStep(4, filled({ routes }), new Set())["routes.0"]).toBeDefined();
    expect(
      validateStep(
        4,
        filled({ routes: [{ ...routes[0]!, path: "x", action: "redirect" }] }),
        new Set(),
      )["routes.0.path"],
    ).toBeDefined();
    expect(
      validateStep(4, filled({ members: [{ address: "", port: "", weight: 1 }] }), new Set())
        .members,
    ).toBeDefined();
    expect(
      validateStep(
        4,
        withProtocol(filled({ members: [{ address: "10.0.0.1", port: "", weight: 1 }] }), "udp"),
        new Set(),
      )["members.0.port"],
    ).toMatch(/UDP/);
    expect(validateStep(4, filled({ pool_choice: "" }), new Set()).pool_choice).toBeDefined();
    const m = { ...filled().monitor, timeout_s: 9, interval_s: 5 };
    expect(validateStep(5, filled({ monitor: m }), new Set()).monitor_timeout).toBeDefined();
    expect(
      validateStep(
        5,
        filled({ monitor: { ...m, timeout_s: 3, type: "udp", payload_hex: "zz" } }),
        new Set(),
      ).monitor_payload,
    ).toBeDefined();
  });
  it("applies protocol rules", () => {
    const u = withProtocol(filled({ algorithm: "random" }), "udp");
    expect(u.ports.every((p) => !p.tls && !p.http2 && !p.proxy_protocol)).toBe(true);
    expect(u.algorithm).toBe("source");
    expect(u.monitor.type).toBe("udp");
    expect(stepTitles("udp")[3]).toBe("Pool");
    expect(stepTitles("http")[3]).toBe("Routes and pools");
    expect(withProtocol(u, "tcp").monitor.type).toBe("tcp");
    expect(downAfterSeconds({ interval_s: 5, fall: 3 })).toBe(15);
  });
  it("builds monitor, pool and service ops in dependency order", () => {
    n = 0;
    const routes = [
      {
        host: "docs.test",
        kind: "prefix" as const,
        path: "/a",
        action: "pool" as const,
        redirect_location: "",
        redirect_code: 301 as const,
      },
      {
        host: "old.test",
        kind: "exact" as const,
        path: "/",
        action: "redirect" as const,
        redirect_location: "https://new.test",
        redirect_code: 301 as const,
      },
    ];
    const b = buildOps(filled({ routes, certificate_ids: ["c1"] }), id);
    expect(b.ops.map((o) => o.object_type)).toEqual(["health_monitor", "pool", "virtual_service"]);
    const pool = b.ops[1]!.body as { health_monitor_id: string; members: { port: number }[] };
    expect(pool.health_monitor_id).toBe(b.monitorId);
    expect(pool.members[0]!.port).toBe(8080);
    const vs = b.ops[2]!.body as {
      default_pool_id: string;
      routes: {
        order: number;
        pool_id: string | null;
        redirect: unknown;
        match: Record<string, unknown>;
      }[];
      vip_pool_id: string;
    };
    expect(vs.default_pool_id).toBe(b.poolId);
    expect(vs.vip_pool_id).toBe("vp1");
    expect(vs.routes[0]).toMatchObject({ order: 1, pool_id: b.poolId, redirect: null });
    expect(vs.routes[1]).toMatchObject({
      order: 2,
      pool_id: null,
      redirect: { location: "https://new.test", code: 301 },
    });
    expect(vs.routes[1]!.match).toMatchObject({ path_exact: "/", path_prefix: null });
  });
  it("reuses an existing pool and monitor, and builds UDP and HTTP monitors", () => {
    const reuse = buildOps(filled({ pool_choice: "p9", monitor_choice: "m9" }), id);
    expect(reuse.ops.map((o) => o.object_type)).toEqual(["virtual_service"]);
    expect((reuse.ops[0]!.body as { default_pool_id: string }).default_pool_id).toBe("p9");
    const http = buildOps(
      filled({ monitor: { ...filled().monitor, type: "http", expect_status: "200, 204" } }),
      id,
    );
    expect((http.ops[0]!.body as { http: { expect_status: number[] } }).http.expect_status).toEqual(
      [200, 204],
    );
    const udp = buildOps(
      withProtocol(filled({ monitor: { ...filled().monitor, payload_hex: "ab" } }), "udp"),
      id,
    );
    const body = udp.ops[0]!.body as Record<string, unknown>;
    expect(body.udp).toEqual({ payload_hex: "ab", require_reply: false });
    expect(body).not.toHaveProperty("rise");
    expect((udp.ops.at(-1)!.body as { routes: unknown[] }).routes).toEqual([]);
    const fixed = buildOps(filled({ vipMode: "fixed" }), id);
    expect(fixed.ops.at(-1)!.body).not.toHaveProperty("vip_pool_id");
  });
});

let posted: { ops?: { object_type: string }[] } | null = null;
let role = "operator";
const server = setupServer(
  http.get("*/api/v1/me", () =>
    HttpResponse.json({
      auth: "session",
      break_glass: false,
      csrf_token: "tok",
      email: null,
      id: "s",
      is_system: false,
      name: "Miguel Rodriguez",
      session: null,
      tenants: [{ id: "t1", name: "acme", role, via: [] }],
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "write", object: "application", min_role: "operator", description: "Change apps." },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.post("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json(
      {
        id: "cs1",
        state: "draft",
        author: "x",
        summary: "x",
        base_revision: 1,
        created_at: "2026-10-10T10:00:00Z",
        ops: [],
      },
      { status: 201 },
    ),
  ),
  http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", async ({ request }) => {
    posted = (await request.json()) as typeof posted;
    return HttpResponse.json({ id: "cs1" });
  }),
  http.get("*/api/v1/tenants/acme/se-groups", () =>
    HttpResponse.json({
      items: [{ id: "g1", name: "se-edge-a", ha_mode: "bgp" }],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/tenants/acme/vip-pools", () =>
    HttpResponse.json({ items: [{ id: "vp1", name: "vip-edge" }], next_cursor: null }),
  ),
  http.post("*/api/v1/tenants/acme/vip-pools/vp1/next-free", () =>
    HttpResponse.json({ address: "192.0.2.77" }),
  ),
  http.get("*/api/v1/tenants/acme/certificates", () =>
    HttpResponse.json({ items: [{ id: "c1", name: "wildcard" }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/pools", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/health-monitors", () =>
    HttpResponse.json({
      items: [
        { id: "m1", name: "http-healthz", type: "http", interval_s: 5, rise: 2, fall: 3 },
        { id: "m2", name: "wg-udp", type: "udp", interval_s: 10, rise: 1, fall: 3 },
      ],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services", () =>
    HttpResponse.json({ items: [{ id: "v1", name: "taken" }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services/:id", () =>
    HttpResponse.json({ title: "x", status: 404, detail: "x" }, { status: 404 }),
  ),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  posted = null;
  role = "operator";
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

async function pickCertificate() {
  const box = await screen.findByRole("combobox", { name: "Certificates" });
  await userEvent.click(box);
  await userEvent.click(await screen.findByRole("option", { name: /wildcard/ }));
}

const next = () => userEvent.click(screen.getByRole("button", { name: /^Next/ }));

describe("create wizard", () => {
  it("walks all six steps and adds the ops to a new draft", async () => {
    const router = renderAt("/t/acme/new");
    await userEvent.type(await screen.findByLabelText("Name"), "taken");
    await pick(await screen.findByLabelText("SE group"), "g1");
    await next();
    expect(
      await screen.findByText("A virtual service with this name already exists."),
    ).toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Name"));
    await userEvent.type(screen.getByLabelText("Name"), "docs-site");
    await next();

    await pick(await screen.findByLabelText("VIP pool"), "vp1");
    await next();
    await screen.findByRole("heading", { name: "Ports and TLS" });
    expect(screen.getByText(/suggested/)).toHaveTextContent("192.0.2.77");
    await pickCertificate();
    await next();

    await screen.findByRole("heading", { name: "Routes and pools" });
    await userEvent.type(screen.getByLabelText("Pool name"), "docs-web");
    await userEvent.type(screen.getByLabelText("Member 1 address"), "10.1.0.1");
    await userEvent.click(screen.getByRole("button", { name: "Add route" }));
    await userEvent.type(screen.getByLabelText("Route 1 host"), "docs.test");
    await next();

    await screen.findByRole("heading", { name: "Health check" });
    await userEvent.type(screen.getByLabelText("Monitor name"), "docs-check");
    expect(screen.getByText(/about 15 s to mark a member down/)).toBeInTheDocument();
    await next();

    await screen.findByRole("heading", { name: "Review" });
    expect(screen.getByText(/3 ops/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add to change set" }));
    await waitFor(() =>
      expect(posted?.ops?.map((o) => o.object_type)).toEqual([
        "health_monitor",
        "pool",
        "virtual_service",
      ]),
    );
    await waitFor(() =>
      expect(router.state.location.pathname).toMatch(/^\/t\/acme\/virtual-services\/.+/),
    );
  });

  it("shows UDP rules: no TLS, no routes, only UDP monitors", async () => {
    renderAt("/t/acme/new");
    await userEvent.click(await screen.findByRole("radio", { name: "UDP" }));
    await userEvent.type(screen.getByLabelText("Name"), "wg");
    await pick(screen.getByLabelText("SE group"), "g1");
    await next();
    await userEvent.click(await screen.findByRole("radio", { name: "Fixed address" }));
    await userEvent.type(screen.getByLabelText("VIP address"), "192.0.2.9");
    await next();
    expect(await screen.findByText(/No TLS, HTTP\/2 or PROXY protocol on UDP/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Certificates")).not.toBeInTheDocument();
    await next();
    await screen.findByRole("heading", { name: "Pool" });
    expect(screen.queryByRole("button", { name: "Add route" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Random" })).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Pool name"), "wg-pool");
    await userEvent.type(screen.getByLabelText("Member 1 address"), "10.2.0.1");
    await next();
    await screen.findByRole("heading", { name: "Health check" });
    await userEvent.click(screen.getByRole("combobox", { name: "Monitor choice" }));
    expect(screen.getByRole("option", { name: /wg-udp/ })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /http-healthz/ })).not.toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByLabelText("Rise")).not.toBeInTheDocument();
  });

  it("viewers cannot add to the change set", async () => {
    role = "viewer";
    renderAt("/t/acme/new");
    await userEvent.type(await screen.findByLabelText("Name"), "x1");
    await pick(await screen.findByLabelText("SE group"), "g1");
    await next();
    await userEvent.click(await screen.findByRole("radio", { name: "Fixed address" }));
    await userEvent.type(screen.getByLabelText("VIP address"), "192.0.2.9");
    await next();
    await pickCertificate();
    await next();
    await userEvent.type(await screen.findByLabelText("Pool name"), "p1");
    await userEvent.type(screen.getByLabelText("Member 1 address"), "10.1.0.1");
    await next();
    await userEvent.type(await screen.findByLabelText("Monitor name"), "m1");
    await next();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Add to change set" })).toBeDisabled(),
    );
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("heading", { name: "Health check" })).toBeInTheDocument();
  });

  it("shows a server problem when adding fails", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", () =>
        HttpResponse.json(
          {
            title: "Validation failed",
            status: 422,
            detail: "bad",
            errors: [{ path: "ops.2.body.vip", code: "x", message: "VIP is outside the network." }],
          },
          { status: 422 },
        ),
      ),
    );
    renderAt("/t/acme/new");
    await userEvent.type(await screen.findByLabelText("Name"), "x1");
    await pick(await screen.findByLabelText("SE group"), "g1");
    await next();
    await userEvent.click(await screen.findByRole("radio", { name: "Fixed address" }));
    await userEvent.type(screen.getByLabelText("VIP address"), "192.0.2.9");
    await next();
    await pickCertificate();
    await next();
    await userEvent.type(await screen.findByLabelText("Pool name"), "p1");
    await userEvent.type(screen.getByLabelText("Member 1 address"), "10.1.0.1");
    await next();
    await userEvent.type(await screen.findByLabelText("Monitor name"), "m1");
    await next();
    const add = await screen.findByRole("button", { name: "Add to change set" });
    await waitFor(() => expect(add).toBeEnabled());
    await userEvent.click(add);
    expect(await screen.findByText(/VIP is outside the network/)).toBeInTheDocument();
  });

  it("exercises the port, route, member and monitor controls", async () => {
    renderAt("/t/acme/new");
    await userEvent.type(await screen.findByLabelText("Name"), "x1");
    await pick(await screen.findByLabelText("SE group"), "g1");
    await userEvent.click(screen.getByRole("radio", { name: "TCP" }));
    await userEvent.click(screen.getByRole("radio", { name: "HTTP" }));
    await next();
    await userEvent.click(await screen.findByRole("radio", { name: "Fixed address" }));
    await userEvent.type(screen.getByLabelText("VIP address"), "192.0.2.9");
    await next();
    await userEvent.click(await screen.findByRole("button", { name: "Add port" }));
    await userEvent.clear(screen.getByLabelText("Port 2"));
    await userEvent.type(screen.getByLabelText("Port 2"), "8080");
    for (const box of screen.getAllByRole("checkbox")) await userEvent.click(box);
    await userEvent.click(screen.getAllByRole("button", { name: "Remove" })[1]!);
    expect(screen.queryByLabelText("Port 2")).not.toBeInTheDocument();
    await pickCertificate();
    await next();
    await screen.findByRole("heading", { name: "Routes and pools" });
    await userEvent.click(screen.getByRole("button", { name: "Add route" }));
    await userEvent.selectOptions(screen.getByLabelText("Route 1 match"), "exact");
    await userEvent.type(screen.getByLabelText("Route 1 path"), "/x");
    await userEvent.selectOptions(screen.getByLabelText("Route 1 action"), "redirect");
    await userEvent.type(screen.getByLabelText("Route 1 redirect location"), "https://a.test");
    await userEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]!);
    await userEvent.selectOptions(screen.getByLabelText("Algorithm"), "leastconn");
    await userEvent.type(screen.getByLabelText("Pool name"), "p1");
    await userEvent.click(screen.getByRole("button", { name: "Add member" }));
    await userEvent.type(screen.getByLabelText("Member 1 address"), "10.1.0.1");
    await userEvent.type(screen.getByLabelText("Member 2 address"), "10.1.0.2");
    await userEvent.clear(screen.getByLabelText("Member 2 weight"));
    await userEvent.type(screen.getByLabelText("Member 2 weight"), "3");
    await userEvent.type(screen.getByLabelText("Member 2 port"), "9000");
    await userEvent.click(screen.getAllByRole("button", { name: "Remove" }).at(-1)!);
    await pick(screen.getByLabelText("Pool choice"), "new");
    await next();
    await pick(await screen.findByLabelText("Monitor choice"), "m1");
    expect(
      screen.getByText("Checks every 5 s. Up after 2 passing, down after 3 failing."),
    ).toBeInTheDocument();
    await pick(screen.getByLabelText("Monitor choice"), "new");
    await userEvent.selectOptions(screen.getByLabelText("Monitor type"), "http");
    await userEvent.clear(screen.getByLabelText("Monitor path"));
    await userEvent.type(screen.getByLabelText("Monitor path"), "/up");
    await userEvent.clear(screen.getByLabelText("Expected status"));
    await userEvent.type(screen.getByLabelText("Expected status"), "204");
    for (const l of ["Interval", "Timeout", "Rise", "Fall"]) {
      await userEvent.clear(screen.getByLabelText(l));
      await userEvent.type(screen.getByLabelText(l), "4");
    }
    await userEvent.type(screen.getByLabelText("Monitor name"), "m-new");
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(await screen.findByRole("heading", { name: "Routes and pools" })).toBeInTheDocument();
  });

  it("existing pool skips the monitor step content", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/pools", () =>
        HttpResponse.json({
          items: [{ id: "p1", name: "web-pool", members: [] }],
          next_cursor: null,
        }),
      ),
    );
    renderAt("/t/acme/new");
    await userEvent.type(await screen.findByLabelText("Name"), "x1");
    await pick(await screen.findByLabelText("SE group"), "g1");
    await next();
    await userEvent.click(await screen.findByRole("radio", { name: "Fixed address" }));
    await userEvent.type(screen.getByLabelText("VIP address"), "192.0.2.9");
    await next();
    await pickCertificate();
    await next();
    await pick(await screen.findByLabelText("Pool choice"), "p1");
    await next();
    expect(await screen.findByText(/keeps its own health check/)).toBeInTheDocument();
  });
});
