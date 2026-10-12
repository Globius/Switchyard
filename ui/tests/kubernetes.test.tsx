import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { syncSummary } from "@/features/kubernetes/Kubernetes";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";
import { pick } from "./pick";

const CLUSTER = {
  id: "k1",
  name: "prod",
  api_url: "https://k8s.test:6443",
  ca_set: true,
  class_name: "switchyard",
  gateway_class: "switchyard",
  enabled: true,
  mode: "nodeport",
  se_group: "edge",
  vip_pool: "v6",
  version: 3,
  last_sync: { created: 2, updated: 1, deleted: 0 },
  last_sync_at: "2026-10-12T10:00:00Z",
  last_sync_error: null,
  last_sync_ok: true,
  waiting_for_change_set: null,
};
let posted: { url: string; body: unknown; ifMatch?: string | null }[] = [];
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
      tenants: [{ id: "t1", name: "acme", role: "tenant-admin", via: [] }],
      features: { lxc_guests: false },
    }),
  ),
  http.get("*/api/v1/permissions", () => HttpResponse.json({ roles: [], permissions: [] })),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/se-groups", () =>
    HttpResponse.json({ items: [{ id: "g", name: "edge" }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/vip-pools", () =>
    HttpResponse.json({ items: [{ id: "p", name: "v6" }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/kubernetes-clusters", () => HttpResponse.json([CLUSTER])),
  http.post("*/api/v1/tenants/acme/kubernetes-clusters", async ({ request }) => {
    posted.push({ url: "create", body: await request.json() });
    return HttpResponse.json(CLUSTER, { status: 201 });
  }),
  http.patch("*/api/v1/tenants/acme/kubernetes-clusters/k1", async ({ request }) => {
    posted.push({
      url: "patch",
      body: await request.json(),
      ifMatch: request.headers.get("if-match"),
    });
    return HttpResponse.json(CLUSTER);
  }),
  http.post("*/api/v1/tenants/acme/kubernetes-clusters/k1/sync", () => {
    posted.push({ url: "sync", body: null });
    return HttpResponse.json({ job_id: "j1" }, { status: 202 });
  }),
  http.post("*/api/v1/tenants/acme/kubernetes-clusters/k1/test", () =>
    HttpResponse.json({
      ok: true,
      detail: "Reached the API server",
      version: "v1.31.0",
      nodes: 3,
      services: 12,
      gateway_api: true,
      permissions: [],
      problems: ["Cannot list endpointslices"],
    }),
  ),
  http.delete("*/api/v1/tenants/acme/kubernetes-clusters/k1", () => {
    posted.push({ url: "delete", body: null });
    return new HttpResponse(null, { status: 204 });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  posted = [];
});
afterAll(() => server.close());

function renderIt() {
  render(
    <ThemeProvider>
      <QueryClientProvider client={createQueryClient()}>
        <RouterProvider
          router={createRouter({
            routeTree,
            history: createMemoryHistory({ initialEntries: ["/t/acme/kubernetes"] }),
          })}
        />
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("kubernetes", () => {
  it("summarises the last sync", () => {
    expect(syncSummary({ ...CLUSTER, last_sync_ok: null, last_sync_at: null })).toBe(
      "Not synced yet",
    );
    expect(syncSummary({ ...CLUSTER, last_sync_ok: false, last_sync_error: "401" })).toBe("401");
    expect(syncSummary(CLUSTER as never)).toContain("2 created");
    expect(syncSummary({ ...CLUSTER, last_sync: { skipped: "change set open" } } as never)).toBe(
      "change set open",
    );
  });

  it("lists clusters and syncs one now", async () => {
    renderIt();
    expect(await screen.findByText("prod")).toBeInTheDocument();
    expect(screen.getByText("edge · v6")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Sync now" }));
    await waitFor(() => expect(posted[0]?.url).toBe("sync"));
  });

  it("connects a cluster with a write-only token", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("button", { name: "Connect a cluster" }));
    await userEvent.type(await screen.findByLabelText("Name"), "stage");
    await userEvent.type(screen.getByLabelText("API server URL"), "https://s.test:6443");
    await userEvent.type(screen.getByLabelText(/Service-account token/), "tok123");
    await pick(screen.getByRole("combobox", { name: "SE group" }), "edge");
    await pick(screen.getByRole("combobox", { name: "VIP pool" }), "v6");
    await userEvent.click(screen.getByLabelText("Ready pods"));
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(posted[0]?.url).toBe("create"));
    expect(posted[0]?.body).toMatchObject({
      name: "stage",
      token: "tok123",
      mode: "pods",
      se_group: "edge",
      vip_pool: "v6",
    });
  });

  it("edits with If-Match, tests the connection and disconnects", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("button", { name: "prod" }));
    await userEvent.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText("Cannot list endpointslices")).toBeInTheDocument();
    expect(screen.getByText(/Kubernetes v1.31.0/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(posted[0]?.url).toBe("patch"));
    expect(posted[0]?.ifMatch).toBe("3");
    expect(posted[0]?.body).not.toHaveProperty("token");
  });

  it("deletes after confirming", async () => {
    renderIt();
    await userEvent.click(await screen.findByRole("button", { name: "prod" }));
    await userEvent.click(await screen.findByRole("button", { name: "Delete" }));
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
    await waitFor(() => expect(posted[0]?.url).toBe("delete"));
  });

  it("shows a server finding on the API URL and keeps the sheet open", async () => {
    server.use(
      http.post("*/api/v1/tenants/acme/kubernetes-clusters", () =>
        HttpResponse.json(
          {
            type: "about:blank",
            title: "Invalid",
            status: 422,
            errors: [{ path: "api_url", message: "Must be https" }],
          },
          { status: 422, headers: { "content-type": "application/problem+json" } },
        ),
      ),
    );
    renderIt();
    await userEvent.click(await screen.findByRole("button", { name: "Connect a cluster" }));
    await userEvent.type(await screen.findByLabelText("Name"), "x");
    await userEvent.type(screen.getByLabelText("API server URL"), "http://k");
    await userEvent.type(screen.getByLabelText(/Service-account token/), "t");
    await userEvent.type(screen.getByLabelText("API server CA"), "PEM");
    await pick(screen.getByRole("combobox", { name: "SE group" }), "edge");
    await pick(screen.getByRole("combobox", { name: "VIP pool" }), "v6");
    await userEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByText("Must be https")).toBeInTheDocument();
  });

  it("marks a failing and a disabled cluster", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/kubernetes-clusters", () =>
        HttpResponse.json([
          { ...CLUSTER, id: "k2", name: "bad", last_sync_ok: false, last_sync_error: "401" },
          { ...CLUSTER, id: "k3", name: "off", enabled: false, mode: "pods" },
          {
            ...CLUSTER,
            id: "k4",
            name: "new",
            last_sync_ok: null,
            last_sync_at: null,
            waiting_for_change_set: "cs1",
          },
        ]),
      ),
    );
    renderIt();
    expect(await screen.findByText("Failing")).toBeInTheDocument();
    expect(screen.getByText("Disabled")).toBeInTheDocument();
    expect(screen.getByText("New")).toBeInTheDocument();
    expect(screen.getByText("Waiting for a change set")).toBeInTheDocument();
  });
});
