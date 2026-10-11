import { QueryClientProvider } from "@tanstack/react-query";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { clearSessionExpired, markSessionExpired } from "@/auth/expired";
import { createQueryClient } from "@/auth/session";
import { SessionExpiredModal } from "@/features/login/SessionExpiredModal";

let meOk = false;
let glass: unknown = null;
const ME = {
  auth: "session",
  break_glass: false,
  csrf_token: "tok",
  email: null,
  id: "s",
  is_system: false,
  name: "Miguel",
  principal: "Miguel",
  session: null,
  tenants: [],
};
const server = setupServer(
  http.get("*/api/v1/auth/providers", () =>
    HttpResponse.json({
      break_glass: true,
      providers: [{ id: "pa", name: "Authentik", protocol: "oidc" }],
    }),
  ),
  http.get("*/api/v1/me", () =>
    meOk
      ? HttpResponse.json(ME)
      : HttpResponse.json(
          { title: "Expired", status: 401, detail: "x", code: "session_expired" },
          { status: 401 },
        ),
  ),
  http.post("*/api/v1/auth/break-glass", async ({ request }) => {
    glass = await request.json();
    meOk = true;
    return HttpResponse.json(ME);
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  meOk = false;
  glass = null;
  act(() => clearSessionExpired());
});
afterAll(() => server.close());

function renderApp() {
  const root = createRootRoute({
    component: () => (
      <>
        <Outlet />
        <SessionExpiredModal />
      </>
    ),
  });
  const page = createRoute({
    getParentRoute: () => root,
    path: "/",
    component: () => <input aria-label="Draft text" defaultValue="" />,
  });
  const router = createRouter({
    routeTree: root.addChildren([page]),
    history: createMemoryHistory({ initialEntries: ["/"] }),
  });
  render(
    <QueryClientProvider client={createQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

describe("session expired modal", () => {
  it("opens over the page without unmounting it, so typed input is kept", async () => {
    renderApp();
    const input = await screen.findByLabelText("Draft text");
    await userEvent.type(input, "keep me");
    act(() => markSessionExpired());
    expect(await screen.findByText("Your session ended")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Continue with Authentik" })).toHaveAttribute(
      "href",
      "/api/v1/auth/login/pa",
    );
    expect(screen.getByLabelText("Draft text", { selector: "input" })).toHaveValue("keep me");
  });

  it("closes after break-glass sign-in", async () => {
    renderApp();
    act(() => markSessionExpired());
    await userEvent.type(await screen.findByLabelText("Username"), "bg");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.type(screen.getByLabelText("Authenticator code"), "123456");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(glass).toEqual({ username: "bg", password: "pw", totp: "123456" }));
    await waitFor(() => expect(screen.queryByText("Your session ended")).not.toBeInTheDocument());
  });

  it("closes when I have signed in elsewhere", async () => {
    renderApp();
    act(() => markSessionExpired());
    await screen.findByText("Your session ended");
    await userEvent.click(screen.getByRole("button", { name: "I have signed in" }));
    expect(screen.getByText("Your session ended")).toBeInTheDocument();
    meOk = true;
    await userEvent.click(screen.getByRole("button", { name: "I have signed in" }));
    await waitFor(() => expect(screen.queryByText("Your session ended")).not.toBeInTheDocument());
  });
});
