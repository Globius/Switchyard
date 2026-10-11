import { QueryClientProvider } from "@tanstack/react-query";
import { pick } from "./pick";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { draftOwnerName, parseAuthor } from "@/features/shell/author";
import { defaultDraftName } from "@/features/shell/draft-write";
import { notApplied } from "@/features/vs/VsOverview";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

describe("author helpers", () => {
  it("parses the API's author strings", () => {
    expect(parseAuthor("token:gitlab-ci-deploy")).toEqual({
      kind: "token",
      label: "gitlab-ci-deploy",
    });
    expect(parseAuthor("user:local:breakglass")).toEqual({
      kind: "break_glass",
      label: "breakglass",
    });
    expect(parseAuthor("user:authentik:abc")).toEqual({
      kind: "person",
      label: "user:authentik:abc",
    });
  });
  it("names drafts for people, tokens and break-glass", () => {
    expect(draftOwnerName({ name: "Miguel Rodriguez" })).toBe("Miguel");
    expect(draftOwnerName({ name: "token:gitlab-ci-deploy", auth: "token" })).toBe(
      "gitlab-ci-deploy",
    );
    expect(draftOwnerName({ name: "breakglass", break_glass: true })).toBe("break-glass");
    expect(defaultDraftName({ name: "x", break_glass: true }, new Date(2026, 9, 10, 10, 42))).toBe(
      "Draft by break-glass, 10:42",
    );
  });
  it("knows which members exist only in the draft", () => {
    const live = [{ id: "p", members: [{ id: "a" }] }];
    expect(notApplied({ id: "p", draft_status: "created" }, "a", live)).toBe(true);
    expect(notApplied({ id: "p", draft_status: "modified" }, "b", live)).toBe(true);
    expect(notApplied({ id: "p", draft_status: "modified" }, "a", live)).toBe(false);
    expect(notApplied({ id: "p", draft_status: "unchanged" }, "b", live)).toBe(false);
    expect(notApplied({ id: "p", draft_status: "modified" }, "b", undefined)).toBe(false);
  });
});

let me: object = {};
let authorOfDraft = "token:ci";
let problems: object[] = [];
let nextFree = "192.0.2.10";
let freeCalls = 0;
const base = {
  auth: "session",
  break_glass: false,
  csrf_token: "tok",
  email: null,
  id: "s",
  is_system: false,
  name: "Miguel Rodriguez",
  principal: "user:x:1",
  session: null,
  tenants: [{ id: "t1", name: "acme", role: "operator", via: [] }],
};
const member = (id: string, address: string, name: string) => ({
  id,
  address,
  port: 80,
  weight: 1,
  admin_state: "enabled",
  name,
});
const server = setupServer(
  http.get("*/api/v1/me", () => HttpResponse.json({ ...base, ...me })),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "edit", object: "draft", min_role: "operator", description: "Edit." },
        { verb: "write", object: "application", min_role: "operator", description: "Write." },
        { verb: "set_state", object: "member", min_role: "operator", description: "State." },
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
                author: authorOfDraft,
                summary: "Draft by someone, 10:42",
                base_revision: 41,
                created_at: "2026-10-10T10:00:00Z",
                op_count: 2,
              },
            ]
          : [],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/tenants/acme/change-sets/cs1/diff", () =>
    HttpResponse.json({
      objects: [],
      problems,
      warnings: [],
      impact: { vips: [], services: [], se_groups: [], hostnames: [] },
      se_groups: [],
    }),
  ),
  http.get("*/api/v1/tenants/acme/se-groups", () =>
    HttpResponse.json({ items: [{ id: "g1", name: "edge", ha_mode: "bgp" }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/vip-pools", () =>
    HttpResponse.json({ items: [{ id: "vp1", name: "vip-edge" }], next_cursor: null }),
  ),
  http.post("*/api/v1/tenants/acme/vip-pools/vp1/next-free", () => {
    freeCalls++;
    return HttpResponse.json({ address: nextFree });
  }),
  http.get("*/api/v1/tenants/acme/certificates", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/policy-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services/v1/logs", () =>
    HttpResponse.json({ available: false, reason: "no log backend", entries: [] }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services/v1/rendered", () =>
    HttpResponse.json({ items: [] }),
  ),
  http.get("*/api/v1/tenants/acme/health-monitors", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services/v1", () =>
    HttpResponse.json({
      id: "v1",
      name: "web",
      protocol: "http",
      vip: "10.0.0.1",
      ports: [{ port: 80 }],
      enabled: true,
      certificate_ids: [],
      routes: [
        { order: 0, match: { host: "a.test" }, pool_id: "p1", redirect: null },
        { order: 1, match: { host: "b.test" }, pool_id: "p1", redirect: null },
      ],
      default_pool_id: "p1",
      se_group_id: "g1",
      version: 1,
      draft_status: "unchanged",
      changed_fields: [],
    }),
  ),
  http.get("*/api/v1/tenants/acme/status/virtual-services", () =>
    HttpResponse.json([
      {
        id: "v1",
        name: "web",
        protocol: "http",
        state: "serving",
        health: "degraded",
        members_total: 3,
        members_up: 2,
      },
    ]),
  ),
  http.get("*/api/v1/tenants/acme/metrics/virtual-services", () =>
    HttpResponse.json({
      available: false,
      reason: "Prometheus is not configured",
      window_s: 900,
      items: [],
    }),
  ),
  http.get("*/api/v1/tenants/acme/pools", ({ request }) => {
    const draft = new URL(request.url).searchParams.has("change_set");
    const members = draft
      ? [member("a", "10.1.0.1", "one"), member("b", "10.1.0.2", "two")]
      : [member("a", "10.1.0.1", "one")];
    return HttpResponse.json({
      items: [
        {
          id: "p1",
          name: "web-pool",
          algorithm: "roundrobin",
          members,
          draft_status: draft ? "modified" : "unchanged",
          version: 1,
          changed_fields: [],
          tenant_id: "t1",
        },
        {
          id: "p2",
          name: "other-pool",
          algorithm: "roundrobin",
          members: [],
          draft_status: "unchanged",
          version: 1,
          changed_fields: [],
          tenant_id: "t1",
        },
      ],
      next_cursor: null,
    });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  me = {};
  authorOfDraft = "token:ci";
  problems = [];
  nextFree = "192.0.2.10";
  freeCalls = 0;
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
const bar = async () => within(await screen.findByRole("region", { name: "Change set" }));

describe("change-set bar author variants", () => {
  it("API token: name, role and a token pill", async () => {
    me = { auth: "token", name: "token:ci", principal: "token:ci" };
    renderAt("/t/acme");
    const b = await bar();
    expect(await b.findByText(/API token · operator/)).toBeInTheDocument();
    expect(b.getByText("token")).toBeInTheDocument();
  });
  it("break-glass: red pill", async () => {
    authorOfDraft = "user:local:breakglass";
    me = { name: "breakglass", break_glass: true, principal: "user:local:breakglass" };
    renderAt("/t/acme");
    const b = await bar();
    expect(await b.findByText(/Break-glass administrator/)).toBeInTheDocument();
    expect(b.getByText("break-glass")).toBeInTheDocument();
  });
  it("person: shows your name for your own draft", async () => {
    authorOfDraft = "user:x:1";
    renderAt("/t/acme");
    expect(await (await bar()).findByText(/Miguel Rodriguez/)).toBeInTheDocument();
  });
  it("shows the problem count linking to the review", async () => {
    authorOfDraft = "user:x:1";
    problems = [
      { path: "virtual_services.web.vip", code: "x", message: "m" },
      { path: "y", code: "x", message: "m" },
    ];
    renderAt("/t/acme");
    const b = await bar();
    const link = await b.findByRole("link", { name: /2 problems/ });
    expect(link).toHaveAttribute("href", "/t/acme/review/cs1");
  });
});

describe("virtual service overview", () => {
  it("numbers routes from 1, shows members up and Not applied, and quotes the metrics reason", async () => {
    authorOfDraft = "user:x:1";
    renderAt("/t/acme/virtual-services/v1");
    expect(await screen.findByText("Degraded · 2 of 3 members up")).toBeInTheDocument();
    const row = (await screen.findByText("a.test")).closest("tr")!;
    expect(within(row).getAllByRole("cell")[0]).toHaveTextContent("1");
    expect(
      within(screen.getByText("b.test").closest("tr")!).getAllByRole("cell")[0],
    ).toHaveTextContent("2");
    expect(await screen.findByText(/Prometheus is not configured/)).toBeInTheDocument();
    const two = (await screen.findByText("10.1.0.2:80")).closest("tr")!;
    await waitFor(() => expect(within(two).getByText("Not applied")).toBeInTheDocument());
    expect(
      within(screen.getByText("10.1.0.1:80").closest("tr")!).queryByText("Not applied"),
    ).not.toBeInTheDocument();
  });
});

describe("virtual service header actions", () => {
  it("Disable and Delete add ops to the draft; the Settings tab carries the draft dot", async () => {
    authorOfDraft = "user:x:1";
    const posted: {
      ops: { action: string; object_id: string; body?: unknown; expected_version: number }[];
    }[] = [];
    server.use(
      http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", async ({ request }) => {
        posted.push((await request.json()) as (typeof posted)[number]);
        return HttpResponse.json({ id: "cs1" });
      }),
      http.get("*/api/v1/tenants/acme/virtual-services/v1", () =>
        HttpResponse.json({
          id: "v1",
          name: "web",
          protocol: "http",
          vip: "10.0.0.1",
          ports: [{ port: 80 }],
          enabled: true,
          certificate_ids: [],
          routes: [],
          policy_set_ids: [],
          default_pool_id: null,
          se_group_id: "g1abcdefgh",
          version: 3,
          draft_status: "modified",
          changed_fields: ["name", "vip"],
        }),
      ),
    );
    const router = renderAt("/t/acme/virtual-services/v1");
    expect(await screen.findByText("Modified in draft · 2 fields")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Settings/ })).toContainElement(
      screen.getAllByLabelText("Changed in draft")[0]!,
    );
    const disable = await screen.findByRole("button", { name: "Disable" });
    await waitFor(() => expect(disable).toBeEnabled());
    await userEvent.click(disable);
    await waitFor(() =>
      expect(posted[0]?.ops[0]).toMatchObject({
        action: "update",
        object_id: "v1",
        expected_version: 3,
        body: { enabled: false },
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    await userEvent.click(await screen.findByRole("button", { name: "Add delete to draft" }));

    await waitFor(() =>
      expect(posted[1]?.ops[0]).toMatchObject({
        action: "delete",
        object_id: "v1",
        expected_version: 3,
      }),
    );
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/acme/virtual-services"));
  });
});

describe("pools filter in the URL and wizard VIP suggestion", () => {
  it("keeps the pools name filter in the URL", async () => {
    const router = renderAt("/t/acme/pools?q=web");
    await screen.findByRole("button", { name: "web-pool" });
    expect(screen.queryByRole("button", { name: "other-pool" })).not.toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Filter by name"));
    await waitFor(() => expect(router.state.location.search).not.toHaveProperty("q"));
    expect(await screen.findByRole("button", { name: "other-pool" })).toBeInTheDocument();
  });

  it("suggests an address when the pool is chosen, labels it, and suggests again", async () => {
    renderAt("/t/acme/new");
    await userEvent.type(await screen.findByLabelText("Name"), "x1");
    await pick(await screen.findByLabelText("SE group"), "g1");
    await userEvent.click(screen.getByRole("button", { name: /^Next/ }));
    await pick(await screen.findByLabelText("VIP pool"), "vp1");
    expect(await screen.findByDisplayValue("192.0.2.10")).toHaveAttribute("readonly");
    expect(screen.getByText("Suggested, not reserved")).toBeInTheDocument();
    expect(
      screen.getByText(/Review blocks the change set that is applied second/),
    ).toBeInTheDocument();
    nextFree = "192.0.2.11";
    await userEvent.click(screen.getByRole("button", { name: "Suggest again" }));
    expect(await screen.findByDisplayValue("192.0.2.11")).toBeInTheDocument();
    expect(freeCalls).toBe(2);
  });
});
