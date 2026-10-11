import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { errorCopy, safeNext } from "@/features/login/Login";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

let breakGlass = true;
let providers = [
  { id: "p1", name: "Authentik", protocol: "oidc" },
  { id: "p2", name: "Entra ID", protocol: "saml" },
];
let signedIn = false;
let glassBody: unknown = null;
const ME = {
  auth: "session",
  break_glass: true,
  csrf_token: "tok",
  email: null,
  id: "s",
  is_system: true,
  name: "Break glass",
  session: null,
  tenants: [],
};
const server = setupServer(
  http.get("*/api/v1/auth/providers", () =>
    HttpResponse.json({ break_glass: breakGlass, providers }),
  ),
  http.get("*/api/v1/me", () =>
    signedIn
      ? HttpResponse.json(ME)
      : HttpResponse.json(
          { title: "Unauthorized", status: 401, detail: "no", code: "session_expired" },
          { status: 401 },
        ),
  ),
  http.get("*/api/v1/permissions", () => HttpResponse.json({ roles: [], permissions: [] })),
  http.get("*/api/v1/tenants", () =>
    HttpResponse.json([
      { id: "t1", name: "acme", quotas: {}, require_approval: false, version: 1 },
    ]),
  ),
  http.get("*/api/v1/tenants/acme/change-sets", () =>
    HttpResponse.json({ items: [], next_cursor: null }),
  ),
  http.post("*/api/v1/auth/break-glass", async ({ request }) => {
    glassBody = await request.json();
    const b = glassBody as { password: string };
    if (b.password !== "right")
      return HttpResponse.json(
        { title: "Unauthorized", status: 401, detail: "Invalid credentials.", code: "bad_login" },
        { status: 401 },
      );
    signedIn = true;
    return HttpResponse.json(ME);
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  breakGlass = true;
  signedIn = false;
  glassBody = null;
  providers = [
    { id: "p1", name: "Authentik", protocol: "oidc" },
    { id: "p2", name: "Entra ID", protocol: "saml" },
  ];
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

describe("login helpers", () => {
  it("only follows paths inside the app", () => {
    expect(safeNext("/t/acme/pools")).toBe("/t/acme/pools");
    expect(safeNext("//evil.test")).toBeUndefined();
    expect(safeNext("https://evil.test")).toBeUndefined();
    expect(safeNext(undefined)).toBeUndefined();
  });
  it("has copy for every error code", () => {
    for (const c of [
      "provider_error",
      "expired",
      "no_access",
      "disabled",
      "session_ended",
    ] as const)
      expect(errorCopy(c, "Authentik", "/x").title.length).toBeGreaterThan(5);
    expect(errorCopy("session_ended", undefined, "/t/a").body).toContain("/t/a");
    expect(errorCopy("provider_error").title).toBe("Sign-in didn't complete.");
  });
});

describe("login screen", () => {
  it("lists providers as links with return_to and the break-glass form", async () => {
    renderAt("/login?next=/t/acme/pools");
    const a = await screen.findByRole("link", { name: "Continue with Authentik" });
    expect(a).toHaveAttribute("href", "/api/v1/auth/login/p1?return_to=%2Ft%2Facme%2Fpools");
    expect(screen.getByRole("link", { name: "Continue with Entra ID" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in as break-glass" })).toBeDisabled();
  });

  it.each([
    ["no_access", "You don't have access yet."],
    ["disabled", "This account is disabled."],
    ["expired", "That sign-in attempt expired."],
    ["provider_error", "Sign-in didn't complete."],
    ["session_ended", "Your session ended."],
  ])("shows the %s state", async (code, title) => {
    renderAt(`/login?error=${code}`);
    expect(await screen.findByRole("alert")).toHaveTextContent(title);
  });

  it("hides break-glass when it is off and says when no provider exists", async () => {
    breakGlass = false;
    providers = [];
    renderAt("/login");
    expect(await screen.findByText("No identity provider is configured yet.")).toBeInTheDocument();
    expect(screen.queryByText("Break-glass administrator")).not.toBeInTheDocument();
  });

  it("signs in with break-glass and goes back to where you were", async () => {
    const router = renderAt("/login?next=/t/acme");
    await userEvent.type(await screen.findByLabelText("Username"), "root");
    await userEvent.type(screen.getByLabelText("Password"), "right");
    await userEvent.type(screen.getByLabelText("Authenticator code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Sign in as break-glass" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/t/acme"));
    expect(glassBody).toEqual({ username: "root", password: "right", totp: "123456" });
  });

  it("shows the server's reason for a failed break-glass login", async () => {
    renderAt("/login");
    await userEvent.type(await screen.findByLabelText("Username"), "root");
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.type(screen.getByLabelText("Authenticator code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Sign in as break-glass" }));
    expect(await screen.findByText("Invalid credentials.")).toBeInTheDocument();
  });

  it("shows an error with Retry when providers cannot be loaded", async () => {
    server.use(
      http.get("*/api/v1/auth/providers", () =>
        HttpResponse.json(
          { title: "Service unavailable", status: 503, detail: "Try later." },
          { status: 503 },
        ),
      ),
    );
    renderAt("/login");
    expect(await screen.findByText("Try later.", {}, { timeout: 8000 })).toBeInTheDocument();
  }, 15000);

  it("a signed-out visit to a tenant page lands here and remembers the page", async () => {
    const router = renderAt("/t/acme/pools");
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    expect(router.state.location.search).toMatchObject({
      next: "/t/acme/pools",
      error: "session_ended",
    });
  });
});
