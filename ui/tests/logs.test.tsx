import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { statusTone } from "@/features/logs/RequestLogs";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";
import { pick } from "./pick";

const ENTRY = (over: Record<string, unknown> = {}) => ({
  at: "2026-10-11T10:00:00Z",
  backend: "pool_web",
  bytes: 120,
  client_ip: "2001:db8::7",
  duration_ms: 42,
  frontend: "vs_web",
  method: "GET",
  path: "/api/items",
  raw: "",
  server: "2001:db8:30::5:8080",
  status: 502,
  ...over,
});
let queries: URLSearchParams[] = [];
let mode: "ok" | "unavailable" | "empty" | "more" = "ok";
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
      tenants: [{ id: "t1", name: "acme", role: "viewer", via: [] }],
      features: { lxc_guests: false },
    }),
  ),
  http.get("*/api/v1/permissions", () => HttpResponse.json({ roles: [], permissions: [] })),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/virtual-services", () =>
    HttpResponse.json({ items: [{ id: "v1", name: "web" }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/logs", ({ request }) => {
    const u = new URL(request.url);
    queries.push(u.searchParams);
    if (mode === "unavailable")
      return HttpResponse.json({
        available: false,
        reason: "Loki is not configured",
        entries: [],
        next_cursor: null,
      });
    if (mode === "empty")
      return HttpResponse.json({ available: true, entries: [], next_cursor: null, reason: null });
    const older = u.searchParams.get("cursor") === "c1";
    return HttpResponse.json({
      available: true,
      reason: null,
      entries: [older ? ENTRY({ path: "/older", status: 200 }) : ENTRY()],
      next_cursor: mode === "more" && !older ? "c1" : null,
    });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  queries = [];
  mode = "ok";
});
afterAll(() => server.close());

function renderIt() {
  render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider
          router={createRouter({
            routeTree,
            history: createMemoryHistory({ initialEntries: ["/t/acme/logs"] }),
          })}
        />
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("request logs", () => {
  it("colours statuses by class", () => {
    expect([200, 301, 404, 503].map(statusTone)).toEqual([
      "healthy",
      "neutral",
      "degraded",
      "down",
    ]);
  });

  it("lists requests with the client, the server that answered and the duration", async () => {
    renderIt();
    expect(await screen.findByText("/api/items")).toBeInTheDocument();
    expect(screen.getByText("2001:db8::7")).toBeInTheDocument();
    expect(screen.getByText("42 ms")).toBeInTheDocument();
    expect(screen.getByText("502")).toBeInTheDocument();
  });

  it("sends the filters to the API", async () => {
    renderIt();
    await screen.findByText("/api/items");
    await pick(screen.getByRole("combobox", { name: "Virtual service" }), "v1");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "5");
    await userEvent.type(screen.getByLabelText("Search"), "items");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      const last = queries[queries.length - 1]!;
      expect(last.get("service")).toBe("v1");
      expect(last.get("status_class")).toBe("5");
      expect(last.get("q")).toBe("items");
      expect(last.get("since")).toBeTruthy();
    });
  });

  it("loads older lines with the cursor", async () => {
    mode = "more";
    renderIt();
    await screen.findByText("/api/items");
    await userEvent.click(screen.getByRole("button", { name: "Load older" }));
    expect(await screen.findByText("/older")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();
  });

  it("shows the unavailable reason and the empty state", async () => {
    mode = "unavailable";
    renderIt();
    expect(await screen.findByText(/Logs unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/Loki is not configured/)).toBeInTheDocument();
  });

  it("says when nothing matched", async () => {
    mode = "empty";
    renderIt();
    expect(await screen.findByText("No requests in this range")).toBeInTheDocument();
  });

  it("marks requests the WAF refused", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/logs", () =>
        HttpResponse.json({
          available: true,
          reason: null,
          next_cursor: null,
          entries: [ENTRY({ waf: true, waf_action: "deny", status: 403 })],
        }),
      ),
    );
    renderIt();
    expect(await screen.findByText("WAF denied")).toBeInTheDocument();
  });
});
