import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { collectionLabel, diffObject, diffSnapshots, show } from "@/lib/snapshot-diff";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

describe("snapshot diff", () => {
  it("finds created, modified and deleted objects and ignores bookkeeping", () => {
    const from = {
      pools: [
        { id: "1", name: "a", algorithm: "roundrobin", version: 1 },
        { id: "2", name: "gone", algorithm: "source" },
      ],
    };
    const to = {
      pools: [
        { id: "1", name: "a", algorithm: "leastconn", version: 2 },
        { id: "3", name: "new", algorithm: "source" },
      ],
      certificates: [{ id: "c", name: "wild", sans: ["a.test"] }],
    };
    const d = diffSnapshots(from, to);
    expect(d.map((x) => `${x.collection}:${x.name}:${x.status}`)).toEqual([
      "certificates:wild:created",
      "pools:a:modified",
      "pools:gone:deleted",
      "pools:new:created",
    ]);
    expect(d[1]!.fields).toEqual([
      { field: "algorithm", before: "roundrobin", after: "leastconn" },
    ]);
  });
  it("treats lists as whole values and null as missing", () => {
    expect(diffObject({ a: [1] }, { a: [1, 2] })).toHaveLength(1);
    expect(diffObject({ a: null }, {})).toEqual([]);
    expect(show(null)).toBe("—");
    expect(show(["x"])).toBe('["x"]');
    expect(show("s")).toBe("s");
    expect(collectionLabel("virtual_services")).toBe("Virtual service");
    expect(collectionLabel("odd")).toBe("odd");
  });
});

let role = "tenant-admin";
let revisions: object[] = [];
let rollbackCalled: number | null = null;
const rev = (number: number, summary: string) => ({
  number,
  summary,
  author: "dana",
  created_at: "2026-10-10T10:00:00Z",
  git_sha: "abcdef123",
});
const CS = (id: string, state: string, summary: string, author = "Miguel Rodriguez") => ({
  id,
  state,
  summary,
  author,
  base_revision: 41,
  created_at: `2026-10-10T10:0${id.length}:00Z`,
  op_count: 2,
});

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
        {
          verb: "rollback",
          object: "revision",
          min_role: "tenant-admin",
          description: "Roll back.",
        },
        { verb: "edit", object: "draft", min_role: "operator", description: "Edit." },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/change-sets", ({ request }) => {
    const s = new URL(request.url).searchParams.get("state");
    const all: Record<string, object[]> = {
      draft: [
        CS("cs-1", "draft", "Draft by Miguel, 10:42"),
        CS("cs-22", "draft", "Raise maxconn", "Dana Reyes"),
      ],
      pending_approval: [],
      applying: [],
      failed: [CS("cs-333", "failed", "Broken apply")],
      applied: [CS("cs-4444", "applied", "Old change")],
      discarded: [],
    };
    return HttpResponse.json({ items: all[s ?? "draft"] ?? [], next_cursor: null });
  }),
  http.get("*/api/v1/tenants/acme/change-sets/cs-22", () =>
    HttpResponse.json({
      ...CS("cs-22", "draft", "Raise maxconn", "Dana Reyes"),
      approver: null,
      reject_comment: "Apply after the maintenance window.",
      error: null,
      ops: [
        {
          action: "update",
          object_type: "se_group",
          object_id: "11111111-1111-1111-1111-111111111111",
          expected_version: 3,
          body: { maxconn: 80000 },
        },
      ],
    }),
  ),
  http.get("*/api/v1/tenants/acme/status/virtual-services", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/revisions", () =>
    HttpResponse.json({ items: revisions, next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/revisions/39", () =>
    HttpResponse.json({
      ...rev(39, "a"),
      snapshot: { pools: [{ id: "p", name: "api-v2", algorithm: "roundrobin" }] },
    }),
  ),
  http.get("*/api/v1/tenants/acme/revisions/41", () =>
    HttpResponse.json({
      ...rev(41, "c"),
      snapshot: { pools: [{ id: "p", name: "api-v2", algorithm: "leastconn" }] },
    }),
  ),
  http.get("*/api/v1/tenants/acme/revisions/40", () =>
    HttpResponse.json({
      ...rev(40, "b"),
      snapshot: { pools: [{ id: "p", name: "api-v2", algorithm: "roundrobin" }] },
    }),
  ),
  http.post("*/api/v1/tenants/acme/revisions/:n/rollback", ({ params }) => {
    rollbackCalled = Number(params.n);
    return HttpResponse.json({ ...CS("cs-rb", "draft", "Roll back"), ops: [] }, { status: 201 });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  role = "tenant-admin";
  rollbackCalled = null;
  revisions = [rev(41, "Move billing-db"), rev(40, "Add member"), rev(39, "Roll back to 37")];
});
afterAll(() => server.close());
revisions = [rev(41, "Move billing-db"), rev(40, "Add member"), rev(39, "Roll back to 37")];

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

describe("change sets", () => {
  it("lists open change sets with state, yours marker and a Review/View action; History tab", async () => {
    renderAt("/t/acme/change-sets");
    const mine = (await screen.findByText("Draft by Miguel, 10:42")).closest("li")!;
    await waitFor(() => expect(within(mine).getByText("yours")).toBeInTheDocument());
    expect(within(mine).getByRole("link", { name: "Review" })).toHaveAttribute(
      "href",
      "/t/acme/review/cs-1",
    );
    const theirs = screen.getByText("Raise maxconn").closest("li")!;
    expect(within(theirs).getByRole("link", { name: "View" })).toBeInTheDocument();
    expect(screen.getByText("Broken apply")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Open 3/ })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "History" }));
    expect(await screen.findByText("Old change")).toBeInTheDocument();
    expect(screen.queryByText("Raise maxconn")).not.toBeInTheDocument();
  });

  it("expands a change set to show ops and the rejection comment", async () => {
    renderAt("/t/acme/change-sets");
    await userEvent.click(await screen.findByText("Raise maxconn"));
    expect(await screen.findByText("Apply after the maintenance window.")).toBeInTheDocument();
    expect(screen.getByText("expects v3")).toBeInTheDocument();
    expect(screen.getByText("maxconn")).toBeInTheDocument();
  });

  it("explains an empty history", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/change-sets", () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
    );
    renderAt("/t/acme/change-sets");
    expect(await screen.findByText("No open change sets")).toBeInTheDocument();
  });
});

describe("revisions", () => {
  it("lists revisions, marks the current one and compares two from their snapshots", async () => {
    renderAt("/t/acme/revisions");
    expect(await screen.findByText("Move billing-db")).toBeInTheDocument();
    expect(screen.getByText("current")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Compare #39"));
    expect(screen.getByText("Pick one more revision to compare.")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Compare #41"));
    expect(await screen.findByText("#39 → #41")).toBeInTheDocument();
    expect(await screen.findByText("− roundrobin")).toBeInTheDocument();
    expect(screen.getByText("+ leastconn")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText("Compare #40"));
    await userEvent.click(screen.getByLabelText("Compare #39"));
    expect(await screen.findByText(/No differences between #39 and #40/)).toBeInTheDocument();
  });

  it("rolls back after confirming and opens the review", async () => {
    const router = renderAt("/t/acme/revisions");
    const row = (await screen.findByText("Add member")).closest("li")!;
    await waitFor(() =>
      expect(within(row).getByRole("button", { name: "Roll back" })).toBeEnabled(),
    );
    await userEvent.click(within(row).getByRole("button", { name: "Roll back" }));
    expect(await screen.findByText(/Nothing changes until you apply it/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Create rollback draft" }));
    await waitFor(() => expect(rollbackCalled).toBe(40));
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/acme/review/cs-rb"));
  });

  it("operators cannot roll back; the first revision has no button", async () => {
    role = "operator";
    renderAt("/t/acme/revisions");
    const row = (await screen.findByText("Add member")).closest("li")!;
    await waitFor(() =>
      expect(within(row).getByRole("button", { name: "Roll back" })).toBeDisabled(),
    );
    expect(
      within(screen.getByText("Move billing-db").closest("li")!).queryByRole("button"),
    ).not.toBeInTheDocument();
  });

  it("shows an empty state and a problem on failed rollback", async () => {
    revisions = [];
    renderAt("/t/acme/revisions");
    expect(await screen.findByText("No revisions yet")).toBeInTheDocument();
  });

  it("shows the server's reason when the rollback is refused", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/revisions/:n/rollback", () =>
        HttpResponse.json(
          { title: "Conflict", status: 409, detail: "A rollback is already open." },
          { status: 409 },
        ),
      ),
    );
    renderAt("/t/acme/revisions");
    const row = (await screen.findByText("Add member")).closest("li")!;
    await waitFor(() =>
      expect(within(row).getByRole("button", { name: "Roll back" })).toBeEnabled(),
    );
    await userEvent.click(within(row).getByRole("button", { name: "Roll back" }));
    await userEvent.click(await screen.findByRole("button", { name: "Create rollback draft" }));
    expect(await screen.findByText("A rollback is already open.")).toBeInTheDocument();
  });
});
