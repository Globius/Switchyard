// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { niceMax, segments } from "@/components/sy/TimeSeries";
import { VsMetrics } from "@/features/vs/VsMetrics";

const T0 = Date.parse("2026-10-11T10:00:00Z");
const pts = (vals: (number | null)[], step = 30) =>
  vals.flatMap((v, i) =>
    v === null ? [] : [{ t: new Date(T0 + i * step * 1000).toISOString(), v }],
  );

let windows: string[] = [];
let mode: "ok" | "gap" | "empty" | "forbidden" | "unavailable" = "ok";
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
      links: { grafana: "https://grafana.example/d/vs" },
    }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services/v1/metrics", ({ request }) => {
    windows.push(new URL(request.url).searchParams.get("window_s") ?? "");
    if (mode === "forbidden")
      return HttpResponse.json(
        {
          title: "Forbidden",
          status: 403,
          detail: "Metrics need a role.",
          required_role: "viewer",
        },
        { status: 403 },
      );
    if (mode === "unavailable")
      return HttpResponse.json({ available: false, reason: "Prometheus is down", metrics: null });
    const step = mode === "gap" ? 600 : 30;
    const series = (vals: (number | null)[]) => (mode === "empty" ? [] : pts(vals, step));
    return HttpResponse.json({
      available: true,
      reason: null,
      metrics: {
        window_s: 3600,
        step_s: step,
        summary: { rps: 379, p95_ms: 43, error_5xx_ratio: 0.005, active_connections: null },
        rps: series([100, 200, 300]),
        p95_ms: series([40, 45, 43]),
        error_5xx_ratio: series([0.01, 0.005, 0.005]),
        active_connections: series(mode === "gap" ? [10, null, null, null, 12] : [10, 11, 12]),
      },
    });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  windows = [];
  mode = "ok";
});
afterAll(() => server.close());

function renderIt() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <VsMetrics tenant="acme" id="v1" groupName="se-edge-a" />
    </QueryClientProvider>,
  );
}

describe("chart helpers", () => {
  it("rounds the axis maximum to 1, 2 or 5 times a power of ten", () => {
    expect(niceMax(0)).toBe(1);
    expect(niceMax(379)).toBe(500);
    expect(niceMax(1504)).toBe(2000);
    expect(niceMax(0.03)).toBeCloseTo(0.05);
  });

  it("splits a series into runs at gaps", () => {
    const runs = segments(pts([1, 2, null, null, 5, 6]), 30);
    expect(runs.map((r) => r.length)).toEqual([2, 2]);
    expect(segments([], 30)).toEqual([]);
  });
});

describe("VS metrics", () => {
  it("draws the four panels with the latest values and a Grafana link", async () => {
    renderIt();
    expect(await screen.findByRole("img", { name: /Requests, per second/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Latency p95, ms/ })).toBeInTheDocument();
    expect(
      screen.getByRole("img", { name: /Server errors \(5xx\), % of responses/ }),
    ).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Active connections, count/ })).toBeInTheDocument();
    expect(screen.getByText("379")).toBeInTheDocument();
    // 5xx ratio is shown as a percentage; a missing summary value is a dash, never 0.
    expect(screen.getAllByText("0.5").length).toBeGreaterThan(0);
    const dash = screen.getAllByText("—");
    expect(dash.length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: /Open in Grafana/ })).toHaveAttribute(
      "href",
      "https://grafana.example/d/vs",
    );
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("refetches for the chosen range", async () => {
    renderIt();
    await screen.findByRole("img", { name: /Requests/ });
    await userEvent.click(screen.getByRole("button", { name: "6 h" }));
    await waitFor(() => expect(windows).toContain("21600"));
  });

  it("shows the values as a table on request", async () => {
    renderIt();
    await screen.findByRole("img", { name: /Requests/ });
    await userEvent.click(screen.getByRole("button", { name: "Table view" }));
    const table = await screen.findByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    expect(within(table).getByText("300")).toBeInTheDocument();
  });

  it("marks a gap instead of joining the line", async () => {
    mode = "gap";
    renderIt();
    await screen.findByRole("img", { name: /Active connections/ });
    expect(await screen.findByText("no data")).toBeInTheDocument();
  });

  it("shows the empty, unavailable and no-access states", async () => {
    mode = "empty";
    const a = renderIt();
    expect(await screen.findByText(/No data for this period/)).toBeInTheDocument();
    a.unmount();
    mode = "unavailable";
    const b = renderIt();
    expect(await screen.findByText(/Metrics unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/Prometheus is down/)).toBeInTheDocument();
    b.unmount();
    mode = "forbidden";
    renderIt();
    expect(await screen.findByText(/No access to metrics/)).toBeInTheDocument();
    expect(screen.getByText(/Metrics need a role\. \(needs viewer\)/)).toBeInTheDocument();
  });
});
