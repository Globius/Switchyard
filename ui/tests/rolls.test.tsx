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
import { phaseLabel, rollBody, summary, type Roll } from "@/features/segroups/Rolls";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const ROLL: Roll = {
  id: "r1",
  se_group_id: "g1",
  status: "paused",
  reason: "edge-2: the replacement could not be deployed",
  window: "02:00-05:00 UTC",
  not_before: null,
  soak_seconds: 300,
  holding: false,
  template_vmid: 9770,
  target_version: "0.2.0",
  steps: [
    { old: "edge-1", new: "edge-1b", phase: "done" },
    { old: "edge-2", new: null, phase: "deploying" },
  ],
  log: ["edge-1: deploying replacement", "paused: the replacement could not be deployed"],
  created_by: "user:admin",
  created_at: "2026-10-13T04:00:00Z",
  updated_at: "2026-10-13T05:00:00Z",
  version: 3,
};

let role: "tenant-admin" | "operator" = "tenant-admin";
let status: Roll["status"] = "paused";
const calls: Record<string, unknown> = {};
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
          verb: "deploy",
          object: "se_group",
          min_role: "tenant-admin",
          description: "Deploy or resize an SE group on Proxmox.",
        },
      ],
    }),
  ),
  http.get("*/api/v1/auth/providers", () =>
    HttpResponse.json({ break_glass: false, providers: [] }),
  ),
  http.get("*/api/v1/tenants", () => HttpResponse.json([{ id: "t1", name: "acme", version: 1 }])),
  http.get("*/api/v1/tenants/acme/se-groups", () =>
    HttpResponse.json({ items: [{ id: "g1", name: "edge" }], next_cursor: null }),
  ),
  http.get("*/api/v1/tenants/acme/rolls", () => HttpResponse.json([{ ...ROLL, status }])),
  http.post("*/api/v1/tenants/acme/rolls/r1", async ({ request }) => {
    calls.control = await request.json();
    return HttpResponse.json({ ...ROLL, status: "running" });
  }),
  http.post("*/api/v1/tenants/acme/se-groups/g1/rolls", async ({ request }) => {
    calls.start = await request.json();
    return HttpResponse.json(ROLL, { status: 201 });
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  role = "tenant-admin";
  status = "paused";
  for (const k of Object.keys(calls)) delete calls[k];
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
}

describe("engine rolls", () => {
  it("describes progress in words", () => {
    expect(summary(ROLL)).toBe(
      "1 of 2 engines replaced. edge-2: the replacement could not be deployed",
    );
    expect(summary({ ...ROLL, status: "running", reason: "" })).toBe(
      "1 of 2 engines replaced. edge-2: Deploying replacement",
    );
    expect(summary({ ...ROLL, status: "done" })).toBe("1 of 2 engines replaced");
    expect(phaseLabel("awaiting_enrollment")).toBe("Waiting for it to enroll");
    expect(phaseLabel("something_new")).toBe("something_new");
  });

  it("validates the form before sending", () => {
    const f = { group: "g1", template: "9770", start: "", end: "", after: "", soak: "0" };
    expect(rollBody({ ...f, template: "x" })).toEqual({
      error: "Give the Proxmox template ID (a number from 100).",
    });
    expect(rollBody({ ...f, start: "02:00" })).toEqual({
      error: "Give both ends of the window, or neither.",
    });
    expect(rollBody({ ...f, soak: "-1" })).toHaveProperty("error");
    expect(rollBody({ ...f, start: "02:00", end: "05:00", soak: "300" })).toEqual({
      body: {
        template_vmid: 9770,
        window_start: "02:00",
        window_end: "05:00",
        start_after: null,
        soak_seconds: 300,
      },
    });
  });

  it("lists rolls with each engine and lets an admin resume", async () => {
    renderAt("/t/acme/rolls");
    expect(await screen.findByRole("heading", { name: "Engine rolls" })).toBeInTheDocument();
    expect(await screen.findByText("Paused")).toBeInTheDocument();
    expect(screen.getByText(/02:00-05:00 UTC/)).toBeInTheDocument();
    expect(screen.getByText(/→ edge-1b · Replaced/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(calls.control).toEqual({ action: "resume" }));
  });

  it("aborts only after confirmation", async () => {
    status = "running";
    renderAt("/t/acme/rolls");
    await userEvent.click(await screen.findByRole("button", { name: "Abort" }));
    expect(calls.control).toBeUndefined();
    await userEvent.click(await screen.findByRole("button", { name: "Abort roll" }));
    await waitFor(() => expect(calls.control).toEqual({ action: "abort" }));
  });

  it("starts a roll from the sheet", async () => {
    renderAt("/t/acme/rolls");
    await userEvent.click(await screen.findByRole("button", { name: "Roll engines…" }));
    await userEvent.type(await screen.findByPlaceholderText("9770"), "9770");
    await userEvent.click(screen.getByRole("button", { name: "Start roll" }));
    await waitFor(() =>
      expect(calls.start).toMatchObject({ template_vmid: 9770, soak_seconds: 0 }),
    );
  });

  it("hides the controls from people who cannot deploy", async () => {
    role = "operator";
    renderAt("/t/acme/rolls");
    expect(await screen.findByText("Paused")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Roll engines…" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Resume" })).toBeNull();
  });

  it("has a nav entry", async () => {
    renderAt("/t/acme/rolls");
    expect(await screen.findByRole("link", { name: "Engine rolls" })).toBeInTheDocument();
  });
});
