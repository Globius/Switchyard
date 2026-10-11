import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { collectionOf, computeConflicts, valueFor } from "@/features/review/conflict";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

describe("computeConflicts", () => {
  it("only conflicts when both sides changed a field to different values", () => {
    const base = { name: "p", algorithm: "roundrobin", tls: false, weight: 1 };
    const theirs = { ...base, algorithm: "leastconn", tls: true, weight: 5 };
    const yours = { ...base, algorithm: "source", weight: 5, name: "q" };
    const { conflicts, yoursOnly } = computeConflicts(base, theirs, yours);
    expect(conflicts.map((c) => c.field)).toEqual(["algorithm"]); // weight: same change on both sides
    expect(yoursOnly).toEqual({ name: "q" }); // tls changed only by them: not ours to resend
    expect(valueFor(conflicts[0]!, "base")).toBe("roundrobin");
    expect(valueFor(conflicts[0]!, "theirs")).toBe("leastconn");
    expect(valueFor(conflicts[0]!, "yours")).toBe("source");
  });
  it("offers Both for lists where each side only added, theirs first", () => {
    const base = { certificate_ids: ["a"] };
    const { conflicts } = computeConflicts(
      base,
      { certificate_ids: ["a", "t"] },
      { certificate_ids: ["a", "y"] },
    );
    expect(conflicts[0]!.both).toEqual(["a", "t", "y"]);
    expect(valueFor(conflicts[0]!, "both")).toEqual(["a", "t", "y"]);
    const removed = computeConflicts(
      base,
      { certificate_ids: [] },
      { certificate_ids: ["a", "y"] },
    );
    expect(removed.conflicts[0]!.both).toBeNull();
  });
  it("treats a missing base as empty and ignores bookkeeping fields", () => {
    const { conflicts } = computeConflicts(
      undefined,
      { name: "a", version: 2 },
      { name: "b", version: 3 },
    );
    expect(conflicts.map((c) => c.field)).toEqual(["name"]);
    expect(collectionOf("virtual_service")).toBe("virtual_services");
    expect(collectionOf("health_monitor")).toBe("health_monitors");
  });
});

let deleted: number[] = [];
let posted: {
  ops: {
    action: string;
    object_id: string;
    body: Record<string, unknown>;
    expected_version: number;
  }[];
} | null = null;
const CS = {
  id: "cs1",
  state: "draft",
  author: "me",
  summary: "d",
  base_revision: 41,
  created_at: "2026-10-10T10:00:00Z",
  approver: null,
  reject_comment: null,
  error: null,
  ops: [
    {
      action: "create",
      object_type: "pool",
      object_id: "other",
      body: { name: "x" },
      expected_version: null,
    },
    {
      action: "update",
      object_type: "pool",
      object_id: "p1",
      body: { algorithm: "source", name: "api" },
      expected_version: 3,
    },
  ],
};
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
      tenants: [{ id: "t1", name: "acme", role: "operator", via: [] }],
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "apply", object: "change_set", min_role: "operator", description: "a" },
        { verb: "edit", object: "draft", min_role: "operator", description: "e" },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () =>
    HttpResponse.json([
      { id: "t1", name: "acme", quotas: {}, require_approval: false, version: 1 },
    ]),
  ),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/change-sets/cs1", () => HttpResponse.json(CS)),
  http.get("*/api/v1/tenants/acme/change-sets/cs1/diff", () =>
    HttpResponse.json({
      objects: [],
      problems: [],
      warnings: [],
      impact: { vips: [], services: [], se_groups: [], hostnames: [] },
      se_groups: [],
    }),
  ),
  http.get("*/api/v1/tenants/acme/jobs", () => HttpResponse.json({ items: [], next_cursor: null })),
  http.post("*/api/v1/tenants/acme/change-sets/cs1/apply", () =>
    HttpResponse.json(
      {
        title: "Stale",
        status: 412,
        detail: "stale",
        conflict: { object_type: "pool", object_id: "p1", expected_version: 3, current_version: 4 },
      },
      { status: 412 },
    ),
  ),
  http.get("*/api/v1/tenants/acme/revisions/41", () =>
    HttpResponse.json({
      number: 41,
      author: "x",
      summary: "s",
      created_at: "x",
      git_sha: null,
      snapshot: {
        pools: [{ id: "p1", name: "api", algorithm: "roundrobin", tls: false, version: 3 }],
      },
    }),
  ),
  http.get("*/api/v1/tenants/acme/pools/p1", ({ request }) => {
    const draft = new URL(request.url).searchParams.has("change_set");
    return HttpResponse.json(
      draft
        ? { id: "p1", name: "api", algorithm: "source", tls: false, version: 4 }
        : { id: "p1", name: "api", algorithm: "leastconn", tls: false, version: 4 },
    );
  }),
  http.delete("*/api/v1/tenants/acme/change-sets/cs1/ops/:index", ({ params }) => {
    deleted.push(Number(params.index));
    return HttpResponse.json(CS);
  }),
  http.post("*/api/v1/tenants/acme/change-sets/cs1/ops", async ({ request }) => {
    posted = (await request.json()) as typeof posted;
    return HttpResponse.json(CS);
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  deleted = [];
  posted = null;
});
afterAll(() => server.close());

function renderAt() {
  render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider
          router={createRouter({
            routeTree,
            history: createMemoryHistory({ initialEntries: ["/t/acme/review/cs1"] }),
          })}
        />
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("conflict resolver in Review", () => {
  it("shows base, theirs and yours per conflicting field and rewrites only that object's op", async () => {
    renderAt();
    await waitFor(() =>
      expect(
        within(screen.getByRole("complementary", { name: "Apply" })).getByRole("button", {
          name: "Apply",
        }),
      ).toBeEnabled(),
    );
    await userEvent.click(
      within(screen.getByRole("complementary", { name: "Apply" })).getByRole("button", {
        name: "Apply",
      }),
    );
    const region = within(await screen.findByRole("region", { name: "Resolve conflicts" }));
    expect(await region.findByText("Resolve 1 conflicts")).toBeInTheDocument();
    expect(region.getByText("roundrobin")).toBeInTheDocument();
    expect(region.getByText("leastconn")).toBeInTheDocument();
    expect(region.getByText("source")).toBeInTheDocument();
    expect(region.queryByRole("radio", { name: "algorithm: Both" })).not.toBeInTheDocument();
    const go = region.getByRole("button", { name: "Use choices and re-review" });
    expect(go).toBeDisabled();
    await userEvent.click(region.getByRole("radio", { name: "algorithm: Theirs" }));
    await waitFor(() => expect(go).toBeEnabled());
    await userEvent.click(go);
    await waitFor(() => expect(deleted).toEqual([1]));
    await waitFor(() =>
      expect(posted?.ops[0]).toMatchObject({
        action: "update",
        object_id: "p1",
        expected_version: 4,
        body: { algorithm: "leastconn" },
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("region", { name: "Resolve conflicts" })).not.toBeInTheDocument(),
    );
  });
});
