import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { ruleSummary, targetSummary } from "@/features/alerts/Alerts";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";
import { pick } from "./pick";

let role: "tenant-admin" | "operator" = "tenant-admin";
let body: Record<string, unknown> | null = null;
let patched: Record<string, unknown> | null = null;
let removed = false;
const CATALOG = {
  chat_formats: ["slack", "teams", "mattermost"],
  email_configured: false,
  event_actions: ["change_set.failed", "change_set.apply"],
  metrics: [{ name: "error_5xx_ratio", label: "5xx ratio", scope: "vs", unit: "%" }],
};
const TARGET = (over: Record<string, unknown> = {}) => ({
  id: "t1",
  name: "ops-hook",
  kind: "webhook",
  config: { url: "https://hooks.example.test/x" },
  secret_ref: null,
  enabled: true,
  created_at: "2026-10-11T10:00:00Z",
  created_by: "dana",
  version: 1,
  ...over,
});
const RULE = {
  id: "r1",
  name: "apply-failed",
  kind: "event",
  spec: { actions: ["change_set.failed"] },
  targets: ["ops-hook"],
  enabled: true,
  created_at: "2026-10-11T10:00:00Z",
  created_by: "dana",
  version: 1,
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
      features: { lxc_guests: false },
    }),
  ),
  http.get("*/api/v1/permissions", () => HttpResponse.json({ roles: [], permissions: [] })),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/alert-catalog", () => HttpResponse.json(CATALOG)),
  http.get("*/api/v1/tenants/acme/alerts", () =>
    HttpResponse.json({
      firing: [
        {
          rule: "apply-failed",
          key: "change_set:1",
          state: "firing",
          since: "2026-10-11T09:00:00Z",
          details: {},
        },
      ],
      deliveries: [
        {
          at: "2026-10-11T09:00:05Z",
          rule: "apply-failed",
          target: "ops-hook",
          kind: "firing",
          status: "failed",
          key: "k",
          error: "HTTP 500",
        },
      ],
    }),
  ),
  http.get("*/api/v1/tenants/acme/alert-rules", () => HttpResponse.json([RULE])),
  http.get("*/api/v1/tenants/acme/alert-targets", () => HttpResponse.json([TARGET()])),
  http.post("*/api/v1/tenants/acme/alert-rules", async ({ request }) => {
    body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ ...RULE, name: "new" }, { status: 201 });
  }),
  http.patch("*/api/v1/tenants/acme/alert-rules/:name", async ({ request }) => {
    patched = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json(RULE);
  }),
  http.delete("*/api/v1/tenants/acme/alert-rules/:name", () => {
    removed = true;
    return new HttpResponse(null, { status: 204 });
  }),
  http.post("*/api/v1/tenants/acme/alert-targets", async ({ request }) => {
    body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json(TARGET({ name: "new" }), { status: 201 });
  }),
  http.post("*/api/v1/tenants/acme/alert-targets/:name/test", () =>
    HttpResponse.json({ ok: false, error: "connection refused" }),
  ),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  role = "tenant-admin";
  body = null;
  patched = null;
  removed = false;
});
afterAll(() => server.close());

function renderIt() {
  render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider
          router={createRouter({
            routeTree,
            history: createMemoryHistory({ initialEntries: ["/t/acme/alerts"] }),
          })}
        />
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("summaries", () => {
  it("describes rules and targets in one line", () => {
    expect(ruleSummary({ kind: "event", spec: { actions: ["a", "b"] } })).toBe("on a, b");
    expect(
      ruleSummary({
        kind: "metric",
        spec: { metric: "error_5xx_ratio", op: "gt", threshold: 5, for_minutes: 10 },
      }),
    ).toBe("error_5xx_ratio > 5 for 10 min");
    expect(ruleSummary({ kind: "cert_expiry", spec: { days: [30, 7] } })).toBe(
      "30, 7 days before expiry",
    );
    expect(targetSummary({ kind: "email", config: { to: ["a@x.test", "b@x.test"] } })).toBe(
      "a@x.test, b@x.test",
    );
    expect(
      targetSummary({ kind: "chat", config: { url: "https://h.test", format: "slack" } }),
    ).toBe("https://h.test · slack");
  });
});

describe("Alerts", () => {
  it("shows what is firing and the failed delivery with its error", async () => {
    renderIt();
    expect(await screen.findByText("change_set:1")).toBeInTheDocument();
    expect(screen.getByText("Firing")).toBeInTheDocument();
    expect(screen.getByText("HTTP 500")).toBeInTheDocument();
  });

  it("adds a metric rule from the catalog and edits or deletes an existing one", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("tab", { name: /Rules/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Add rule" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Name"), "five-xx");
    await userEvent.click(dlg.getByRole("radio", { name: "A metric" }));
    await pick(await dlg.findByRole("combobox", { name: "Metric" }), "error_5xx_ratio");
    await userEvent.type(dlg.getByLabelText("Threshold"), "5");
    await userEvent.type(dlg.getByRole("combobox", { name: "Targets" }), "ops{enter}");
    await userEvent.click(dlg.getByRole("button", { name: "Add rule" }));
    await waitFor(() =>
      expect(body).toMatchObject({
        name: "five-xx",
        kind: "metric",
        spec: { metric: "error_5xx_ratio", op: "gt", threshold: 5, for_minutes: 5 },
        targets: ["ops-hook"],
        enabled: true,
      }),
    );
  });

  it("edits a rule's enabled flag and deletes it after a confirm", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("tab", { name: /Rules/ }));
    expect(await screen.findByText("on change_set.failed")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "apply-failed" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.click(dlg.getByRole("switch", { name: "Enabled" }));
    await userEvent.click(dlg.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(patched).toMatchObject({ enabled: false, targets: ["ops-hook"] }));
  });

  it("deletes a rule", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("tab", { name: /Rules/ }));
    await userEvent.click(await screen.findByRole("button", { name: "apply-failed" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.click(dlg.getByRole("button", { name: "Delete" }));
    await userEvent.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Delete rule" }),
    );
    await waitFor(() => expect(removed).toBe(true));
  });

  it("adds a chat target, warns that email cannot send, and reports a failed test message", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("tab", { name: /Targets/ }));
    expect(await screen.findByText("https://hooks.example.test/x")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add target" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.type(dlg.getByLabelText("Name"), "team-chat");
    await userEvent.click(dlg.getByRole("radio", { name: /Chat/ }));
    await userEvent.type(dlg.getByLabelText("URL"), "https://chat.example.test/hook");
    await userEvent.selectOptions(dlg.getByLabelText("Format"), "teams");
    await userEvent.click(dlg.getByRole("button", { name: "Add target" }));
    await waitFor(() =>
      expect(body).toMatchObject({
        name: "team-chat",
        kind: "chat",
        config: { url: "https://chat.example.test/hook", format: "teams" },
        secret_ref: null,
      }),
    );
  });

  it("tests an existing target and shows why it was not delivered", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("tab", { name: /Targets/ }));
    await userEvent.click(await screen.findByRole("button", { name: "ops-hook" }));
    const dlg = within(await screen.findByRole("dialog"));
    await userEvent.click(dlg.getByRole("button", { name: "Send a test message" }));
    expect(await dlg.findByText(/Not delivered: connection refused/)).toBeInTheDocument();
  });

  it("is read-only for an operator", async () => {
    role = "operator";
    renderIt();
    await userEvent.click(await screen.findByRole("tab", { name: /Rules/ }));
    await screen.findByText("apply-failed");
    expect(screen.getByRole("button", { name: "Add rule" })).toBeDisabled();
  });
});
