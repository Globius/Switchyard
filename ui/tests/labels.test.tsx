// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import {
  chipsToLabels,
  isSelector,
  labelChips,
  parseSelectors,
  validateLabelChip,
} from "@/lib/labels";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

describe("label helpers", () => {
  it("round-trips chips and labels, sorted by key", () => {
    expect(labelChips({ team: "pay", env: "prod", flag: "" })).toEqual([
      "env=prod",
      "flag",
      "team=pay",
    ]);
    expect(chipsToLabels(["env=prod", "flag"])).toEqual({ env: "prod", flag: "" });
    expect(labelChips(null)).toEqual([]);
  });
  it("validates chips like the API", () => {
    expect(validateLabelChip("env=prod")).toBeNull();
    expect(validateLabelChip("flag")).toBeNull();
    expect(validateLabelChip("Env=prod")).toMatch(/Keys use/);
    expect(validateLabelChip("env=Prod")).toMatch(/Values use/);
    expect(validateLabelChip("")).toMatch(/Keys use/);
  });
  it("keeps only selectors the API accepts, so half-typed text never errors", () => {
    expect(parseSelectors("env=prod, team !legacy tier!=db")).toEqual([
      "env=prod",
      "team",
      "!legacy",
      "tier!=db",
    ]);
    expect(parseSelectors("Env, env=")).toEqual(["env="]);
    expect(parseSelectors(undefined)).toEqual([]);
    expect(isSelector("!")).toBe(false);
    expect(isSelector("a=B")).toBe(false);
  });
});

const ME = {
  auth: "session",
  break_glass: false,
  csrf_token: "tok",
  email: "m@example.test",
  id: "s1",
  is_system: false,
  name: "Miguel",
  principal: "Miguel",
  session: null,
  tenants: [{ id: "t1", name: "acme", role: "operator", via: [] }],
};
const pool = (o: object) => ({
  id: "p0",
  name: "x",
  tenant_id: "t1",
  version: 1,
  algorithm: "roundrobin",
  tls: false,
  tls_verify: false,
  backend_protocol: "http1",
  members: [],
  labels: {},
  draft_status: "unchanged",
  changed_fields: [],
  ...o,
});

const seen: string[][] = [];
const server = setupServer(
  http.get("*/api/v1/me", () => HttpResponse.json(ME)),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "write", object: "application", min_role: "operator", description: "Change." },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/health-monitors", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/virtual-services", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/pools", ({ request }) => {
    const wanted = new URL(request.url).searchParams.getAll("label");
    seen.push(wanted);
    const all = [
      pool({ id: "p1", name: "web", labels: { env: "prod", tier: "web" } }),
      pool({ id: "p2", name: "db", labels: { env: "dev" } }),
    ];
    return HttpResponse.json({
      items: wanted.includes("env=prod") ? all.slice(0, 1) : all,
      next_cursor: null,
    });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  seen.length = 0;
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

describe("label filter on the pools list", () => {
  it("shows label chips and filters by clicking one", async () => {
    const router = renderAt("/t/acme/pools");
    await screen.findByRole("button", { name: "web" });
    expect(screen.getByRole("button", { name: "db" })).toBeInTheDocument();
    await userEvent.click(screen.getByTitle("Show only env=prod"));
    await waitFor(() => expect(router.state.location.search).toMatchObject({ label: "env=prod" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "db" })).not.toBeInTheDocument(),
    );
    expect(seen.at(-1)).toEqual(["env=prod"]);
    expect(screen.getByLabelText("Label")).toHaveValue("env=prod");
  });

  it("sends the selectors typed in the filter box", async () => {
    renderAt("/t/acme/pools");
    await screen.findByRole("button", { name: "web" });
    await userEvent.type(screen.getByLabelText("Label"), "env=prod");
    await waitFor(() => expect(seen.at(-1)).toEqual(["env=prod"]));
  });

  it("explains an empty labelled result", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/pools", () =>
        HttpResponse.json({ items: [], next_cursor: null }),
      ),
    );
    renderAt("/t/acme/pools?label=env%3Dnone");
    expect(await screen.findByText("No pools match")).toBeInTheDocument();
  });
});
