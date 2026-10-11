import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { blocked, countsLine, parseUnified, phaseOf } from "@/features/review/model";
import { seconds } from "@/features/review/JobProgress";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

describe("review model", () => {
  it("derives the phase from change set and job", () => {
    expect(phaseOf("applied", undefined)).toBe("applied");
    expect(phaseOf("discarded", undefined)).toBe("discarded");
    expect(phaseOf("pending_approval", undefined)).toBe("pending_approval");
    expect(phaseOf("draft", undefined)).toBe("ready");
    expect(phaseOf("applying", undefined)).toBe("applying");
    expect(phaseOf("failed", undefined)).toBe("failed");
    expect(phaseOf("draft", { state: "dead", retrying: false })).toBe("gave_up");
    expect(phaseOf("draft", { state: "queued", retrying: true })).toBe("retrying");
    expect(phaseOf("draft", { state: "queued", retrying: false })).toBe("applying");
    expect(phaseOf("draft", { state: "running", retrying: false })).toBe("applying");
    expect(phaseOf("draft", { state: "succeeded", retrying: false, ok: false })).toBe("reverted");
    expect(
      phaseOf("draft", { state: "succeeded", retrying: false, result: { ok: false } as never }),
    ).toBe("reverted");
    expect(phaseOf("draft", { state: "failed", retrying: false })).toBe("failed");
    expect(phaseOf("draft", { state: "succeeded", retrying: false, ok: true })).toBe("ready");
  });
  it("counts, parses diffs and blocks on problems", () => {
    expect(
      countsLine([{ status: "created" }, { status: "modified" }, { status: "modified" }]),
    ).toBe("1 created, 2 modified");
    const l = parseUnified("--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n same\n");
    expect(l.map((x) => x.kind)).toEqual(["hunk", "del", "add", "ctx"]);
    expect(blocked(undefined)).toBe(true);
    expect(blocked({ problems: [{}], warnings: [] } as never)).toBe(true);
    expect(blocked({ problems: [], warnings: [{}] } as never)).toBe(false);
    expect(seconds("2026-10-10T10:00:00Z", "2026-10-10T10:00:02Z")).toBe(2);
    expect(seconds(null, null)).toBeNull();
  });
});

const STAGES = (states: string[]) =>
  ["lock", "render", "validate", "canary", "rollout", "commit"].map((name, i) => ({
    name,
    state: states[i] ?? "pending",
    started_at: "2026-10-10T10:00:00Z",
    finished_at: states[i] === "succeeded" ? "2026-10-10T10:00:01Z" : null,
    error: null,
  }));
const base = (o: object = {}) => ({
  id: "cs1",
  state: "draft",
  author: "me",
  summary: "Raise maxconn",
  base_revision: 41,
  created_at: "2026-10-10T10:00:00Z",
  approver: null,
  reject_comment: null,
  error: null,
  ops: [],
  ...o,
});
const DIFF = {
  objects: [
    { type: "pool", id: "p1", name: "api-v2", status: "modified", changed_fields: ["algorithm"] },
    { type: "virtual_service", id: "v1", name: "docs", status: "created", changed_fields: [] },
    { type: "virtual_service", id: "v2", name: "old", status: "deleted", changed_fields: [] },
  ],
  problems: [] as object[],
  warnings: [] as object[],
  impact: {
    vips: ["10.0.0.1"],
    services: [{ id: "v1", name: "docs" }],
    se_groups: [{ id: "g", name: "edge" }],
    hostnames: ["docs.test"],
  },
  se_groups: [
    {
      id: "g",
      name: "edge",
      error: null,
      files: [
        {
          path: "haproxy.cfg",
          status: "modified",
          diff: "@@ -1 +1 @@\n-balance roundrobin\n+balance leastconn\n",
          before: "",
          after: "",
        },
      ],
    },
  ],
};

let role = "operator";
let principal = "me";
let cs = base();
let diff = DIFF;
let requireApproval = false;
let job: object | null = null;
const calls: string[] = [];
let rejectBody: unknown = null;
let applyBody: unknown = null;
const PERMS = [
  { verb: "apply", object: "change_set", min_role: "operator", description: "Apply a change set." },
  {
    verb: "request_approval",
    object: "change_set",
    min_role: "operator",
    description: "Request approval.",
  },
  {
    verb: "approve",
    object: "change_set",
    min_role: "tenant-admin",
    description: "Approve a change set.",
  },
  {
    verb: "reject",
    object: "change_set",
    min_role: "tenant-admin",
    description: "Reject a change set.",
  },
  {
    verb: "apply_bypass",
    object: "break_glass",
    min_role: "system-admin",
    description: "Bypass approval.",
  },
];
const server = setupServer(
  http.get("*/api/v1/me", () =>
    HttpResponse.json({
      auth: "session",
      break_glass: false,
      csrf_token: "tok",
      email: null,
      id: "s",
      is_system: role === "system-admin",
      name: "Me",
      principal,
      session: null,
      tenants: [{ id: "t1", name: "acme", role, via: [] }],
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: PERMS,
    }),
  ),
  http.get("*/api/v1/tenants", () =>
    HttpResponse.json([
      { id: "t1", name: "acme", quotas: {}, require_approval: requireApproval, version: 1 },
    ]),
  ),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/change-sets/cs1", () => HttpResponse.json(cs)),
  http.get("*/api/v1/tenants/acme/change-sets/cs1/diff", () => HttpResponse.json(diff)),
  http.get("*/api/v1/tenants/acme/pools/p1", ({ request }) =>
    HttpResponse.json({
      id: "p1",
      name: "api-v2",
      algorithm: new URL(request.url).searchParams.has("change_set") ? "leastconn" : "roundrobin",
      version: 1,
    }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services/v1", () =>
    HttpResponse.json({ id: "v1", name: "docs", protocol: "http", enabled: true }),
  ),
  http.get("*/api/v1/tenants/acme/jobs", () =>
    HttpResponse.json({ items: job ? [job] : [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/jobs/j1", () => HttpResponse.json(job)),
  http.post("*/api/v1/tenants/acme/change-sets/cs1/apply", async ({ request }) => {
    calls.push("apply");
    applyBody = await request.json().catch(() => null);
    job = {
      id: "j1",
      kind: "apply_change_set",
      state: "running",
      retrying: false,
      attempts: 1,
      max_attempts: 3,
      created_at: "x",
      change_set_id: "cs1",
      stages: STAGES(["succeeded", "succeeded", "running"]),
    };
    return HttpResponse.json({ job_id: "j1" }, { status: 202 });
  }),
  http.post("*/api/v1/tenants/acme/change-sets/cs1/request-approval", () => {
    calls.push("request");
    cs = base({ state: "pending_approval" });
    return HttpResponse.json(cs);
  }),
  http.post("*/api/v1/tenants/acme/change-sets/cs1/approve", () => {
    calls.push("approve");
    return HttpResponse.json({ job_id: "j1" }, { status: 202 });
  }),
  http.post("*/api/v1/tenants/acme/change-sets/cs1/reject", async ({ request }) => {
    calls.push("reject");
    rejectBody = await request.json();
    return HttpResponse.json(base());
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  role = "operator";
  principal = "me";
  cs = base();
  diff = DIFF;
  requireApproval = false;
  job = null;
  calls.length = 0;
  rejectBody = null;
  applyBody = null;
});
afterAll(() => server.close());

function renderAt(path = "/t/acme/review/cs1") {
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
const panel = async () => within(await screen.findByRole("complementary", { name: "Apply" }));

describe("review screen", () => {
  it("shows objects with before and after values computed from live and draft reads", async () => {
    renderAt();
    expect(await screen.findByRole("heading", { name: "Review change set" })).toBeInTheDocument();
    expect(
      await screen.findByText(/3 changes: 1 created, 1 modified, 1 deleted/),
    ).toBeInTheDocument();
    expect(await screen.findByText("− roundrobin")).toBeInTheDocument();
    expect(screen.getByText("+ leastconn")).toBeInTheDocument();
    expect(screen.getByText(/This object is removed/)).toBeInTheDocument();
    expect(screen.getByText("old")).toHaveClass("line-through");
  });

  it("config tab shows + and − lines; impact tab shows what it touches", async () => {
    renderAt();
    await userEvent.click(await screen.findByRole("tab", { name: "Config" }));
    expect(await screen.findByText("balance leastconn")).toBeInTheDocument();
    expect(screen.getByText("haproxy.cfg")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Impact" }));
    expect(await screen.findByText("10.0.0.1")).toBeInTheDocument();
    expect(screen.getByText("docs.test")).toBeInTheDocument();
    expect(screen.getByText(/No problems found/)).toBeInTheDocument();
  });

  it("applies and shows the stages as the job runs", async () => {
    renderAt();
    const btn = await (await panel()).findByRole("button", { name: "Apply" });
    await waitFor(() => expect(btn).toBeEnabled());
    await userEvent.click(btn);
    await waitFor(() => expect(calls).toEqual(["apply"]));
    expect(applyBody).toEqual({});
    expect(
      await (await panel()).findByRole("list", { name: "Apply progress" }),
    ).toBeInTheDocument();
    expect((await panel()).getByText("Applying")).toBeInTheDocument();
    expect((await panel()).getByText("validate")).toBeInTheDocument();
  });

  it("blocks on validator problems and requires acknowledging warnings", async () => {
    diff = {
      ...DIFF,
      problems: [
        {
          path: "virtual_services.docs.vip",
          code: "vip_outside_network",
          message: "VIP is outside the network.",
        },
      ],
    };
    renderAt();
    expect(await (await panel()).findByText(/1 blocking validator problems/)).toBeInTheDocument();
    expect((await panel()).getByRole("button", { name: "Apply" })).toBeDisabled();
    await userEvent.click(screen.getByRole("tab", { name: /Impact/ }));
    expect(await screen.findByText(/Blocking: VIP is outside the network/)).toBeInTheDocument();
  });

  it("warnings need an acknowledgement before applying", async () => {
    diff = {
      ...DIFF,
      warnings: [
        { path: "x", code: "cert_san_mismatch", message: "Certificate does not cover the host." },
      ],
    };
    renderAt();
    const apply = await (await panel()).findByRole("button", { name: "Apply" });
    expect(apply).toBeDisabled();
    await userEvent.click(
      (await panel()).getByRole("checkbox", { name: /reviewed 1 validator warnings/ }),
    );
    await waitFor(() => expect(apply).toBeEnabled());
  });

  it("viewers cannot apply", async () => {
    role = "viewer";
    renderAt();
    const apply = await (await panel()).findByRole("button", { name: "Apply" });
    await waitFor(() => expect(apply).toBeDisabled());
  });

  it("in an approval tenant the button is Request approval, and the request is sent", async () => {
    requireApproval = true;
    renderAt();
    const btn = await (await panel()).findByRole("button", { name: "Request approval" });
    await waitFor(() => expect(btn).toBeEnabled());
    expect((await panel()).getByText(/requires approval/)).toBeInTheDocument();
    await userEvent.click(btn);
    await waitFor(() => expect(calls).toEqual(["request"]));
  });

  it("break-glass apply needs a reason and sends it", async () => {
    requireApproval = true;
    role = "system-admin";
    renderAt();
    await userEvent.click(await (await panel()).findByRole("button", { name: /Apply anyway/ }));
    await userEvent.type(await screen.findByLabelText("Break-glass reason"), "Controller outage");
    await userEvent.click(screen.getByRole("button", { name: "Apply anyway" }));
    await waitFor(() => expect(applyBody).toEqual({ break_glass_reason: "Controller outage" }));
  });

  it("the author of a pending change set cannot approve it", async () => {
    cs = base({ state: "pending_approval" });
    requireApproval = true;
    renderAt();
    expect(
      await (await panel()).findByText(/You wrote this change set, so you can't approve it/),
    ).toBeInTheDocument();
    expect(
      (await panel()).queryByRole("button", { name: "Approve and apply" }),
    ).not.toBeInTheDocument();
  });

  it("another tenant-admin can approve, or reject with a required comment", async () => {
    cs = base({ state: "pending_approval", author: "dana" });
    requireApproval = true;
    role = "tenant-admin";
    renderAt();
    const approve = await (await panel()).findByRole("button", { name: "Approve and apply" });
    await waitFor(() => expect(approve).toBeEnabled());
    await userEvent.click((await panel()).getByRole("button", { name: "Reject" }));
    const reject = (await panel()).getByRole("button", { name: "Reject" });
    expect(reject).toBeDisabled();
    await userEvent.type((await panel()).getByLabelText("Rejection comment"), "After the window");
    await userEvent.click(reject);
    await waitFor(() => expect(rejectBody).toEqual({ comment: "After the window" }));
    await userEvent.click(approve);
    await waitFor(() => expect(calls).toContain("approve"));
  });

  it("operators see Approve disabled with the reason", async () => {
    cs = base({ state: "pending_approval", author: "dana" });
    renderAt();
    const approve = await (await panel()).findByRole("button", { name: "Approve and apply" });
    await waitFor(() => expect(approve).toBeDisabled());
  });

  it("shows a reverted apply with its failed stage and Apply again", async () => {
    job = {
      id: "j1",
      kind: "apply_change_set",
      state: "succeeded",
      ok: false,
      retrying: false,
      attempts: 1,
      max_attempts: 3,
      created_at: "x",
      change_set_id: "cs1",
      error: "bk_api has 0 healthy servers after 30 s",
      stages: STAGES(["succeeded", "succeeded", "succeeded", "failed", "skipped", "skipped"]),
    };
    renderAt();
    expect(await (await panel()).findByText("Reverted")).toBeInTheDocument();
    expect((await panel()).getByRole("alert")).toHaveTextContent("0 healthy servers");
    expect((await panel()).getByRole("button", { name: "Apply again" })).toBeInTheDocument();
    expect((await panel()).getByText(/will not retry on its own/)).toBeInTheDocument();
  });

  it("distinguishes retrying and gave up", async () => {
    job = {
      id: "j1",
      kind: "apply_change_set",
      state: "queued",
      retrying: true,
      attempts: 2,
      max_attempts: 3,
      created_at: "x",
      change_set_id: "cs1",
      stages: STAGES([]),
    };
    renderAt();
    expect(await (await panel()).findByText("Retrying after error")).toBeInTheDocument();
  });

  it("gave up shows Dead · gave up", async () => {
    job = {
      id: "j1",
      kind: "apply_change_set",
      state: "dead",
      retrying: false,
      attempts: 3,
      max_attempts: 3,
      created_at: "x",
      change_set_id: "cs1",
      error: "agent unreachable",
      stages: STAGES(["succeeded", "failed"]),
    };
    renderAt();
    expect(await (await panel()).findByText("Dead · gave up")).toBeInTheDocument();
  });

  it("an applied or discarded change set has nothing to review", async () => {
    cs = base({ state: "applied" });
    renderAt();
    expect(await screen.findByText(/there is nothing left to review/)).toBeInTheDocument();
    expect((await panel()).getByText("Applied")).toBeInTheDocument();
  });

  it("explains a 412 conflict", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/change-sets/cs1/apply", () =>
        HttpResponse.json(
          {
            title: "Stale",
            status: 412,
            detail: "stale",
            conflict: {
              object_type: "pool",
              object_id: "p1",
              expected_version: 1,
              current_version: 2,
            },
          },
          { status: 412 },
        ),
      ),
    );
    renderAt();
    const btn = await (await panel()).findByRole("button", { name: "Apply" });
    await waitFor(() => expect(btn).toBeEnabled());
    await userEvent.click(btn);
    expect(
      await screen.findByText("Someone else changed an object in this draft"),
    ).toBeInTheDocument();
  });

  it("shows a problem when apply is refused", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/change-sets/cs1/apply", () =>
        HttpResponse.json(
          { title: "Forbidden", status: 403, detail: "Approval is required." },
          { status: 403 },
        ),
      ),
    );
    renderAt();
    const btn = await (await panel()).findByRole("button", { name: "Apply" });
    await waitFor(() => expect(btn).toBeEnabled());
    await userEvent.click(btn);
    expect(await screen.findByText("Approval is required.")).toBeInTheDocument();
  });

  it("refreshes the header and the review when an apply finishes", async () => {
    cs = base({ state: "applying" });
    job = {
      id: "j1",
      kind: "apply_change_set",
      state: "running",
      retrying: false,
      attempts: 1,
      max_attempts: 3,
      created_at: "x",
      change_set_id: "cs1",
      stages: STAGES(["succeeded", "running"]),
    };
    renderAt();
    expect(await screen.findByText(/nothing left to review/)).toBeInTheDocument();
    expect((await screen.findAllByText("Applying")).length).toBeGreaterThan(0);
    cs = base({ state: "failed", error: "boom" });
    job = {
      id: "j1",
      kind: "apply_change_set",
      state: "succeeded",
      ok: false,
      retrying: false,
      attempts: 1,
      max_attempts: 3,
      created_at: "x",
      change_set_id: "cs1",
      error: "boom",
      stages: STAGES(["succeeded", "failed"]),
    };
    await waitFor(
      () => expect(screen.queryByText(/nothing left to review/)).not.toBeInTheDocument(),
      { timeout: 8000 },
    );
    expect(screen.getAllByText("Failed").length).toBeGreaterThan(0);
    expect(await screen.findByRole("tab", { name: /Objects/ })).toBeInTheDocument();
  });
});
