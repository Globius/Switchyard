import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { pickOwnDraft, type ChangeSetSummary } from "@/features/shell/draft";
import { initials } from "@/features/shell/UserMenu";
import { applyTheme } from "@/lib/theme";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const ME = {
  auth: "session",
  break_glass: false,
  csrf_token: "tok",
  email: "m@example.test",
  id: "s1",
  is_system: false,
  name: "Miguel Rodriguez",
  session: null,
  tenants: [
    { id: "t1", name: "acme", role: "viewer", via: [{ provider: "p", group: "sy-acme-view" }] },
  ],
};
const DRAFT = {
  id: "cs1",
  state: "draft",
  author: "Miguel Rodriguez",
  summary: "Draft by Miguel, 10:42",
  base_revision: 41,
  created_at: "2026-10-10T10:42:00Z",
  op_count: 4,
};

const server = setupServer(
  http.get("*/api/v1/me", () => HttpResponse.json(ME)),
  http.get("*/api/v1/permissions", () =>
    HttpResponse.json({
      roles: ["viewer", "operator", "tenant-admin", "system-admin"],
      permissions: [
        { verb: "edit", object: "draft", min_role: "operator", description: "Edit a draft." },
      ],
    }),
  ),
  http.get("*/api/v1/tenants", () =>
    HttpResponse.json([
      { id: "t1", name: "acme", quotas: {}, require_approval: true, version: 1 },
      { id: "t2", name: "lab", quotas: {}, require_approval: false, version: 1 },
    ]),
  ),
  http.get("*/api/v1/tenants/acme/change-sets", ({ request }) =>
    HttpResponse.json({
      items: new URL(request.url).searchParams.get("state") === "draft" ? [DRAFT] : [],
      next_cursor: null,
    }),
  ),
  http.get("*/api/v1/tenants/acme/search", () =>
    HttpResponse.json({
      query: "api",
      truncated: false,
      hits: [
        {
          type: "virtual_service",
          id: "v1",
          name: "api-gateway",
          matched: "name",
          detail: "10.0.0.1",
        },
      ],
    }),
  ),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  localStorage.clear();
  applyTheme("light");
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

describe("shell helpers", () => {
  const cs = (o: Partial<ChangeSetSummary>) => ({ ...DRAFT, ...o }) as ChangeSetSummary;
  it("picks only the caller's open draft", () => {
    const me = { name: "Miguel Rodriguez", email: "m@example.test" };
    expect(pickOwnDraft([cs({ author: "someone" })], me)).toBeUndefined();
    expect(pickOwnDraft([cs({ state: "applied" })], me)).toBeUndefined();
    expect(pickOwnDraft([cs({ author: "m@example.test" })], me)?.id).toBe("cs1");
    expect(pickOwnDraft([cs({ state: "pending_approval" })], me)?.id).toBe("cs1");
    expect(pickOwnDraft([cs({})], undefined)).toBeUndefined();
  });
  it("makes initials", () => {
    expect(initials("Miguel Rodriguez")).toBe("MR");
    expect(initials("  ada ")).toBe("A");
  });
  it("applies the dark class", () => {
    applyTheme("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    applyTheme("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });
});

describe("app shell", () => {
  it("shows tenant, nav and the change-set bar with a disabled Discard for a viewer", async () => {
    renderAt("/t/acme");
    expect(await screen.findByText("Draft by Miguel, 10:42")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /switch tenant/i })).toHaveTextContent("acme");
    expect(screen.getByRole("link", { name: "Virtual services" })).toBeInTheDocument();
    expect(screen.getByText("4 changes")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Review & apply" })).toHaveAttribute(
      "href",
      "/t/acme/review/cs1",
    );
    expect(screen.getByRole("button", { name: "Discard" })).toBeDisabled();
    expect(screen.queryByRole("link", { name: "Admin" })).not.toBeInTheDocument();
  });

  it("lists tenants with the approval badge in the switcher", async () => {
    renderAt("/t/acme");
    await userEvent.click(await screen.findByRole("button", { name: /switch tenant/i }));
    expect(await screen.findByText("approval on")).toBeInTheDocument();
    expect(screen.getByText("lab")).toBeInTheDocument();
  });

  it("opens the palette with Ctrl+K and searches", async () => {
    renderAt("/t/acme");
    await screen.findByText("Draft by Miguel, 10:42");
    await userEvent.keyboard("{Control>}k{/Control}");
    await userEvent.type(await screen.findByPlaceholderText("Jump to any object"), "api");
    expect(await screen.findByText("api-gateway")).toBeInTheDocument();
    expect(screen.getByText("Review my draft")).toBeInTheDocument();
  });

  it("user menu shows roles with their source, refreshes access, switches theme and signs out", async () => {
    let refreshed = false;
    let out = false;
    server.use(
      http.post("*/api/v1/auth/refresh", () => {
        refreshed = true;
        return HttpResponse.json({});
      }),
      http.post("*/api/v1/auth/logout", () => {
        out = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderAt("/t/acme");
    await userEvent.click(await screen.findByRole("button", { name: "User menu" }));
    expect(await screen.findByText("via sy-acme-view")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Refresh access" }));
    await waitFor(() => expect(refreshed).toBe(true));
    await userEvent.click(screen.getByRole("button", { name: "dark" }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem("sy-theme")).toBe("dark");
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    await waitFor(() => expect(out).toBe(true));
    applyTheme("light");
  });

  it("theme toggle in the top bar flips light and dark", async () => {
    renderAt("/t/acme");
    await userEvent.click(await screen.findByRole("button", { name: "Switch to dark theme" }));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Switch to light theme" }));
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("switching tenant and opening a search hit navigate", async () => {
    const router = renderAt("/t/acme");
    await userEvent.click(await screen.findByRole("button", { name: /switch tenant/i }));
    await userEvent.type(await screen.findByPlaceholderText("Find tenant"), "la");
    await userEvent.click(await screen.findByText("lab"));
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/lab"));
  });

  it("opens a virtual service from the palette", async () => {
    const router = renderAt("/t/acme");
    await screen.findByText("Draft by Miguel, 10:42");
    await userEvent.keyboard("{Control>}k{/Control}");
    await userEvent.type(await screen.findByPlaceholderText("Jump to any object"), "api");
    await userEvent.click(await screen.findByText("api-gateway"));
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/acme/virtual-services/v1"));
  });

  it("shows awaiting approval and no Review link target change for a pending draft", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/change-sets", ({ request }) =>
        HttpResponse.json({
          items:
            new URL(request.url).searchParams.get("state") === "pending_approval"
              ? [{ ...DRAFT, state: "pending_approval" }]
              : [],
          next_cursor: null,
        }),
      ),
    );
    renderAt("/t/acme");
    expect(await screen.findByText("Awaiting approval")).toBeInTheDocument();
  });

  it("sends a signed-out visitor to the login page", async () => {
    server.use(
      http.get("*/api/v1/me", () =>
        HttpResponse.json(
          { title: "Unauthorized", status: 401, detail: "no", code: "session_expired" },
          { status: 401 },
        ),
      ),
    );
    const router = renderAt("/t/acme");
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
  });
});
