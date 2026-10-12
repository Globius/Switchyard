import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { detailText, firesIn, ruleSummary, targetSummary } from "@/features/alerts/Alerts";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

let role = "tenant-admin";
let posted: { path: string; body: unknown }[] = [];
const T = {
  id: "t1",
  name: "ops-chat",
  kind: "chat",
  config: { url: "https://hooks.example.test/services/T000/B000/SECRET", format: "slack" },
  secret_ref: null,
  enabled: true,
  version: 1,
  created_by: "a",
  created_at: "2026-10-12T08:00:00Z",
};
const R = {
  id: "r1",
  name: "certs",
  kind: "cert_expiry",
  spec: { days: [30, 14, 7] },
  targets: ["ops-chat"],
  enabled: true,
  version: 1,
  created_by: "a",
  created_at: "2026-10-12T08:00:00Z",
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
      name: "Miguel",
      principal: "Miguel",
      session: null,
      tenants: [{ id: "t1", name: "acme", role, via: [] }],
    }),
  ),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        {
          verb: "write",
          object: "infrastructure",
          min_role: "tenant-admin",
          description: "Infra.",
        },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/alert-catalog", () =>
    HttpResponse.json({
      metrics: [{ name: "vs_5xx_ratio", label: "Share of 5xx responses", unit: "%", scope: "vs" }],
      event_actions: ["change_set.failed"],
      chat_formats: ["slack", "teams", "mattermost"],
      email_configured: false,
    }),
  ),
  http.get("*/api/v1/tenants/acme/alert-targets", () => HttpResponse.json([T])),
  http.get("*/api/v1/tenants/acme/alert-rules", () => HttpResponse.json([R])),
  http.get("*/api/v1/tenants/acme/alerts", () =>
    HttpResponse.json({
      firing: [
        {
          rule: "certs",
          key: "cert:shop",
          state: "firing",
          since: "2026-10-12T08:00:00Z",
          details: {},
        },
      ],
      deliveries: [
        {
          rule: "certs",
          target: "ops-chat",
          key: "cert:shop",
          kind: "firing",
          status: "failed",
          error: "the receiver answered HTTP 500",
          at: "2026-10-12T08:00:00Z",
        },
      ],
    }),
  ),
  http.post("*/api/v1/tenants/acme/alert-targets", async ({ request }) => {
    posted.push({ path: "target", body: await request.json() });
    return HttpResponse.json(T, { status: 201 });
  }),
  http.post("*/api/v1/tenants/acme/alert-targets/ops-chat/test", () =>
    HttpResponse.json({ ok: false, error: "the receiver answered HTTP 403" }),
  ),
  http.post("*/api/v1/tenants/acme/alert-rules", async ({ request }) => {
    posted.push({ path: "rule", body: await request.json() });
    return HttpResponse.json(R, { status: 201 });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  role = "tenant-admin";
  posted = [];
});
afterAll(() => server.close());

function renderIt() {
  const router = createRouter({
    routeTree,
    history: createMemoryHistory({ initialEntries: ["/t/acme/alerts"] }),
  });
  render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("alerts", () => {
  it("describes targets and rules without showing a chat webhook URL", () => {
    expect(targetSummary(T)).toBe("slack · hooks.example.test");
    expect(targetSummary({ kind: "email", config: { to: ["a@x.test", "b@x.test"] } })).toBe(
      "a@x.test, b@x.test",
    );
    expect(ruleSummary(R)).toBe("30, 14, 7 days before expiry");
    expect(
      ruleSummary({
        kind: "metric",
        spec: { metric: "vs_5xx_ratio", op: "gt", threshold: 5, for_minutes: 5, scope: "web" },
      }),
    ).toBe("vs_5xx_ratio > 5 for 5 min · web");
  });

  it("shows what is firing, the targets, the rules and the failed delivery with its reason", async () => {
    renderIt();
    expect((await screen.findAllByText("cert:shop", { selector: "td" })).length).toBe(2);
    expect((await screen.findAllByText("ops-chat", { selector: "td" })).length).toBeGreaterThan(0);
    expect(screen.getByText("slack · hooks.example.test")).toBeInTheDocument();
    expect(screen.queryByText(/SECRET/)).not.toBeInTheDocument();
    expect(screen.getByText("30, 14, 7 days before expiry")).toBeInTheDocument();
    expect(await screen.findByText("the receiver answered HTTP 500")).toBeInTheDocument();
  });

  it("sends a test and shows the outcome", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("button", { name: "Send test" }));
    expect(await screen.findByText("the receiver answered HTTP 403")).toBeInTheDocument();
  });

  it("adds a chat target and a metric rule", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("button", { name: "Add target" }));
    await userEvent.type(screen.getByLabelText("Name"), "pager");
    await userEvent.type(screen.getByLabelText("URL"), "https://hooks.example.test/x");
    await userEvent.click(screen.getByRole("button", { name: "Add target" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]!.body).toEqual({
      name: "pager",
      kind: "chat",
      config: { url: "https://hooks.example.test/x", format: "slack" },
      secret_ref: null,
      enabled: true,
    });
    await userEvent.click(await screen.findByRole("button", { name: "Add rule" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Name"), "errors");
    await userEvent.selectOptions(within(dialog).getByLabelText("Watch"), "metric");
    await userEvent.clear(within(dialog).getByLabelText("Threshold"));
    await userEvent.type(within(dialog).getByLabelText("Threshold"), "2");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add rule" }));
    await waitFor(() => expect(posted).toHaveLength(2));
    expect(posted[1]!.body).toEqual({
      name: "errors",
      kind: "metric",
      spec: { metric: "vs_5xx_ratio", op: "gt", threshold: 2, for_minutes: 5 },
      targets: ["ops-chat"],
      enabled: true,
    });
  });

  it("lets a viewer read everything and change nothing", async () => {
    role = "viewer";
    renderIt();
    expect((await screen.findAllByText("ops-chat", { selector: "td" })).length).toBeGreaterThan(0);
    await waitFor(() => expect(screen.getByRole("button", { name: "Add target" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Send test" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add rule" })).toBeDisabled();
  });
});

describe("pending alerts", () => {
  it("says when a pending alert fires", () => {
    const now = new Date("2026-10-12T10:00:00Z");
    expect(firesIn("2026-10-12T10:04:10Z", now)).toBe("pending, fires in 5 min");
    expect(firesIn("2026-10-12T09:59:00Z", now)).toBe("pending, fires now");
  });

  it("writes the value against the threshold", () => {
    expect(detailText({ value: 92, threshold: 90, unit: "%" })).toBe("92 of 90 %");
    expect(detailText({ value: 3 })).toBe("3");
    expect(detailText({})).toBe("");
  });
});
