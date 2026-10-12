import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { EngineMetrics, enginePanels } from "@/features/segroups/EngineMetrics";
import { GrafanaLink, landingFor } from "@/features/vs/GrafanaLink";

const T0 = Date.parse("2026-10-11T10:00:00Z");
const pts = (vals: number[]) =>
  vals.map((v, i) => ({ t: new Date(T0 + i * 30_000).toISOString(), v }));
const GIB = 2 ** 30;

const METRICS = {
  window_s: 3600,
  step_s: 30,
  summary: {
    cpu_percent: 32,
    vcpus: 4,
    memory_used_bytes: 2.6 * GIB,
    memory_total_bytes: 8 * GIB,
    haproxy_sessions: 1980,
    traffic_in_bps: 147e6,
  },
  cpu_percent: pts([30, 32, 27]),
  memory_used_bytes: pts([2.5 * GIB, 2.6 * GIB, 2.6 * GIB]),
  haproxy_sessions: pts([1900, 1980, 2000]),
  traffic_in_bps: pts([140e6, 147e6, 150e6]),
};

let embedded = false;
let launches: unknown[] = [];
let engineAnswer: () => Response = () =>
  HttpResponse.json({ available: true, reason: null, metrics: METRICS });
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
      links: embedded
        ? { grafana: "https://grafana.example", grafana_embedded: true }
        : { grafana: "https://grafana.example" },
    }),
  ),
  http.get("*/api/v1/tenants/acme/engines/e1/metrics", () => engineAnswer()),
  http.post("*/api/v1/tenants/acme/grafana/launch", async ({ request }) => {
    launches.push(await request.json());
    return HttpResponse.json({
      url: "/grafana/launch?ticket=sygt_x",
      expires_at: "2026-10-12T10:00:00Z",
      tenant_id: "t1",
    });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  embedded = false;
  launches = [];
  vi.restoreAllMocks();
  engineAnswer = () => HttpResponse.json({ available: true, reason: null, metrics: METRICS });
});
afterAll(() => server.close());

const wrap = (ui: React.ReactNode) =>
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      {ui}
    </QueryClientProvider>,
  );

describe("engine metrics", () => {
  it("scales bytes to GiB and bits to Mbit/s and names the engine's size in the units", () => {
    const set = enginePanels(METRICS);
    const by = Object.fromEntries(set.panels.map((p) => [p.key, p]));
    expect(by.cpu!.unit).toBe("% of 4 vCPU");
    expect(by.memory!.unit).toBe("GiB of 8");
    expect(by.memory!.now).toBeCloseTo(2.6);
    expect(by.memory!.points[1]!.v).toBeCloseTo(2.6);
    expect(by.traffic!.now).toBeCloseTo(147);
    expect(by.sessions!.now).toBe(1980);
  });

  it("a missing value is a dash, not zero", () => {
    const set = enginePanels({
      ...METRICS,
      summary: {
        cpu_percent: null,
        vcpus: null,
        memory_used_bytes: null,
        memory_total_bytes: null,
      },
    });
    expect(set.panels.every((p) => p.now == null)).toBe(true);
    expect(set.panels[0]!.unit).toBe("% of all vCPUs");
  });

  it("draws CPU, memory, sessions and traffic for this engine only", async () => {
    wrap(<EngineMetrics tenant="acme" engineId="e1" />);
    expect(await screen.findByRole("img", { name: /CPU, % of 4 vCPU/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Memory used, GiB of 8/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /HAProxy sessions, current/ })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: /Traffic in, Mbit\/s/ })).toBeInTheDocument();
    expect(screen.getByText("this engine only")).toBeInTheDocument();
    expect(screen.getByText("1,980")).toBeInTheDocument();
  });

  it("shows the API's reason when there is no metrics backend", async () => {
    engineAnswer = () =>
      HttpResponse.json({ available: false, reason: "no metrics backend is configured" });
    wrap(<EngineMetrics tenant="acme" engineId="e1" />);
    expect(await screen.findByText(/Metrics unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/no metrics backend is configured/)).toBeInTheDocument();
  });
});

describe("Open in Grafana", () => {
  it("is a plain link when the controller does not proxy Grafana", async () => {
    wrap(<GrafanaLink tenant="acme" service="web" />);
    expect(await screen.findByRole("link", { name: /Open in Grafana/ })).toHaveAttribute(
      "href",
      "https://grafana.example",
    );
  });

  it("asks for a one-time link scoped to the tenant and opens it in a new tab", async () => {
    embedded = true;
    const tab = { opener: {}, location: { href: "" }, close: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    wrap(<GrafanaLink tenant="acme" service="web" />);
    await userEvent.click(await screen.findByRole("button", { name: /Open in Grafana/ }));
    await waitFor(() => expect(tab.location.href).toBe("/grafana/launch?ticket=sygt_x"));
    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(tab.opener).toBeNull();
    expect(launches).toEqual([{ landing: "/d/sy-tenant-services?var-service=vs_web" }]);
  });

  it("closes the tab and says why when the launch fails", async () => {
    embedded = true;
    server.use(
      http.post("*/api/v1/tenants/acme/grafana/launch", () =>
        HttpResponse.json(
          { title: "Bad Gateway", status: 502, detail: "Grafana is not available: boom" },
          { status: 502 },
        ),
      ),
    );
    const tab = { opener: {}, location: { href: "" }, close: vi.fn() };
    vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    wrap(<GrafanaLink tenant="acme" />);
    await userEvent.click(await screen.findByRole("button", { name: /Open in Grafana/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Grafana is not available: boom");
    expect(tab.close).toHaveBeenCalled();
    expect(tab.location.href).toBe("");
  });

  it("builds the landing path and encodes the service name", () => {
    expect(landingFor()).toBe("/d/sy-tenant-services");
    expect(landingFor("a b")).toBe("/d/sy-tenant-services?var-service=vs_a%20b");
  });
});
