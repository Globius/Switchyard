import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { defaultDraftName } from "@/features/shell/draft-write";
import { buildPatch, errorsByField, messagesFor, toEditable } from "@/features/vs/settings-model";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const VS = {
  id: "v1",
  name: "web",
  tenant_id: "t1",
  version: 3,
  protocol: "http",
  vip: "10.0.0.1",
  vip_pool_id: null,
  se_group_id: "g1",
  ports: [{ port: 443, tls: true, http2: true, proxy_protocol: false, https_redirect_port: null }],
  certificate_ids: [],
  enabled: true,
  default_pool_id: null,
  routes: [],
  policy_set_ids: [],
  enforce_allow_list: false,
  draft_status: "unchanged",
  changed_fields: [] as string[],
};

let role = "operator";
let hasDraft = false;
let patchResponse: () => Response = () =>
  HttpResponse.json({ change_set_id: "cs1", object_id: "v1" }, { status: 202 });
let seen: { url?: string; ifMatch?: string | null; body?: unknown; opened?: unknown } = {};
let vsBody: object = VS;
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
      principal: "Miguel Rodriguez",
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
  http.get("*/api/v1/tenants/acme/change-sets", ({ request }) =>
    HttpResponse.json({
      items:
        hasDraft && new URL(request.url).searchParams.get("state") === "draft"
          ? [
              {
                id: "cs1",
                state: "draft",
                author: "Miguel Rodriguez",
                summary: "Draft",
                base_revision: 1,
                created_at: "2026-10-10T10:00:00Z",
                op_count: 1,
              },
            ]
          : [],
      next_cursor: null,
    }),
  ),
  http.post("*/api/v1/tenants/acme/change-sets", async ({ request }) => {
    seen.opened = await request.json();
    hasDraft = true;
    return HttpResponse.json(
      {
        id: "cs1",
        state: "draft",
        author: "Miguel Rodriguez",
        summary: "x",
        base_revision: 1,
        created_at: "2026-10-10T10:00:00Z",
        ops: [],
      },
      { status: 201 },
    );
  }),
  http.get("*/api/v1/tenants/acme/virtual-services/v1", () => HttpResponse.json(vsBody)),
  http.patch("*/api/v1/tenants/acme/virtual-services/v1", async ({ request }) => {
    seen = {
      ...seen,
      url: request.url,
      ifMatch: request.headers.get("if-match"),
      body: await request.json(),
    };
    return patchResponse();
  }),
  http.get("*/api/v1/schema/virtual-service", () =>
    HttpResponse.json({
      protocol: "http",
      algorithms: [],
      member_port_required: false,
      monitor_types: [],
      pool_forbidden: [],
      port_forbidden: [],
      service_forbidden: [],
    }),
  ),
  http.get("*/api/v1/tenants/acme/pools", () =>
    HttpResponse.json({ items: [{ id: "p1", name: "web-pool", members: [] }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/certificates", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/se-groups", () =>
    HttpResponse.json({ items: [{ id: "g1", name: "se-edge-a" }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/policy-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/status/virtual-services", () => HttpResponse.json([])),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  role = "operator";
  hasDraft = false;
  seen = {};
  vsBody = VS;
  patchResponse = () =>
    HttpResponse.json({ change_set_id: "cs1", object_id: "v1" }, { status: 202 });
});
afterAll(() => server.close());

async function enabled(label: string) {
  const el = await screen.findByLabelText(label);
  await waitFor(() => expect(el).toBeEnabled());
  return el;
}

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

describe("settings model", () => {
  it("builds a patch of changed fields only", () => {
    const base = toEditable(VS as never);
    const next = { ...base, name: "web2", ports: [{ ...base.ports[0]!, port: 8443 }] };
    expect(Object.keys(buildPatch(base, next))).toEqual(["name", "ports"]);
    expect(buildPatch(base, base)).toEqual({});
  });
  it("groups errors by path and finds children", () => {
    const e = errorsByField([
      { path: "ports.0.port", message: "taken" },
      { path: "name", message: "bad" },
    ]);
    expect(messagesFor(e, "ports")).toEqual(["taken"]);
    expect(messagesFor(e, "name")).toEqual(["bad"]);
    expect(messagesFor(e, "routes")).toEqual([]);
  });
  it("names drafts like the handoff", () => {
    expect(defaultDraftName("Miguel Rodriguez", new Date(2026, 9, 10, 9, 5))).toBe(
      "Draft by Miguel, 09:05",
    );
  });
});

describe("virtual service settings", () => {
  it("opens a draft on first edit and sends only changed fields with If-Match", async () => {
    renderAt("/t/acme/virtual-services/v1/settings");
    const name = await enabled("Name");
    const save = screen.getByRole("button", { name: "Add to change set" });
    expect(save).toBeDisabled();
    await userEvent.clear(name);
    await userEvent.type(name, "web2");
    await waitFor(() => expect(save).toBeEnabled());
    await userEvent.click(save);
    await waitFor(() => expect(seen.body).toEqual({ name: "web2" }));
    expect(seen.ifMatch).toBe('"3"');
    expect(seen.url).toContain("change_set=cs1");
    expect(seen.opened).toEqual({ summary: expect.stringMatching(/^Draft by Miguel, \d\d:\d\d$/) });
  });

  it("maps 422 field errors onto the input", async () => {
    patchResponse = () =>
      HttpResponse.json(
        {
          title: "Invalid",
          status: 422,
          detail: "x",
          errors: [
            {
              path: "ports.0.port",
              code: "collision",
              message: "Port 443 is already used on this VIP.",
            },
          ],
        },
        { status: 422 },
      );
    renderAt("/t/acme/virtual-services/v1/settings");
    const port = await enabled("Port 1");
    await userEvent.clear(port);
    await userEvent.type(port, "80");
    await userEvent.click(screen.getByRole("button", { name: "Add to change set" }));
    expect((await screen.findAllByText("Port 443 is already used on this VIP.")).length).toBe(2);
  });

  it("shows the changed-by-someone-else banner on 412", async () => {
    patchResponse = () =>
      HttpResponse.json({ title: "Stale", status: 412, detail: "stale" }, { status: 412 });
    renderAt("/t/acme/virtual-services/v1/settings");
    await userEvent.type(await enabled("Name"), "x");
    await userEvent.click(screen.getByRole("button", { name: "Add to change set" }));
    expect(
      await screen.findByText("This service changed while you were editing"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add to change set" })).toBeDisabled();
  });

  it("marks drafted fields with the live value and a Revert action", async () => {
    hasDraft = true;
    vsBody = { ...VS, name: "web-new", draft_status: "modified", changed_fields: ["name"] };
    server.use(
      http.get("*/api/v1/tenants/acme/virtual-services/v1", ({ request }) =>
        HttpResponse.json(new URL(request.url).searchParams.has("change_set") ? vsBody : VS),
      ),
    );
    renderAt("/t/acme/virtual-services/v1/settings");
    expect((await screen.findAllByLabelText("Changed in draft")).length).toBeGreaterThan(0);
    expect(await screen.findByText(/Live: "web"/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Revert" }));
    expect(screen.getByLabelText("Name")).toHaveValue("web");
  });

  it("viewers cannot edit", async () => {
    role = "viewer";
    renderAt("/t/acme/virtual-services/v1/settings");
    await waitFor(() => expect(screen.getByLabelText("Name")).toBeDisabled());
    expect(screen.getByRole("button", { name: "Add to change set" })).toBeDisabled();
  });

  it("edits routes: add, redirect, reorder and remove", async () => {
    vsBody = {
      ...VS,
      routes: [
        { order: 1, match: { host: "a.test" }, pool_id: "p1", redirect: null },
        { order: 2, match: { host: "b.test" }, pool_id: "p1", redirect: null },
      ],
    };
    renderAt("/t/acme/virtual-services/v1/settings");
    await enabled("Route 1 host");
    await userEvent.click(await screen.findByRole("button", { name: "Move route 2 up" }));
    expect(screen.getByLabelText("Route 1 host")).toHaveValue("b.test");
    await userEvent.click(screen.getAllByRole("checkbox", { name: "Redirect" })[0]!);
    expect(screen.getByLabelText("Route 1 redirect location")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add route" }));
    expect(screen.getByLabelText("Route 3 host")).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Remove" }).at(-1)!);
    expect(screen.queryByLabelText("Route 3 host")).not.toBeInTheDocument();
  });

  it("hides fields the protocol forbids (UDP)", async () => {
    vsBody = { ...VS, protocol: "udp" };
    server.use(
      http.get("*/api/v1/schema/virtual-service", () =>
        HttpResponse.json({
          protocol: "udp",
          algorithms: [],
          member_port_required: true,
          monitor_types: ["udp"],
          pool_forbidden: [],
          port_forbidden: ["tls"],
          service_forbidden: ["routes", "certificate_ids"],
        }),
      ),
    );
    renderAt("/t/acme/virtual-services/v1/settings");
    await screen.findByLabelText("Name");
    await waitFor(() => expect(screen.queryByText("Routes")).not.toBeInTheDocument());
    expect(screen.queryByText("Certificates and policies")).not.toBeInTheDocument();
  });

  it("adds and removes ports, toggles flags, and discards edits", async () => {
    renderAt("/t/acme/virtual-services/v1/settings");
    await enabled("Port 1");
    await userEvent.click(screen.getByRole("button", { name: "Add port" }));
    expect(screen.getByLabelText("Port 2")).toHaveValue(80);
    const boxes = screen.getAllByRole("checkbox", { name: "PROXY" });
    await userEvent.click(boxes[1]!);
    expect(boxes[1]).toBeChecked();
    expect(screen.getByText("2 unsaved fields".replace("2", "1"))).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: "Remove" })[1]!);
    expect(screen.queryByLabelText("Port 2")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Discard edits" }));
    expect(screen.getByRole("button", { name: "Add to change set" })).toBeDisabled();
  });

  it("changes SE group, VIP, enabled and default pool", async () => {
    renderAt("/t/acme/virtual-services/v1/settings");
    await userEvent.selectOptions(await enabled("SE group"), "g1");
    await userEvent.type(screen.getByLabelText("VIP"), "1");
    await userEvent.click(screen.getByRole("checkbox", { name: "Enabled" }));
    await userEvent.selectOptions(screen.getByLabelText("Default pool"), "p1");
    expect(screen.getByText("3 unsaved fields")).toBeInTheDocument();
  });
});
