import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { setSessionExpiredHandler } from "@/api/client";
import {
  createQueryClient,
  loginUrl,
  useBreakGlassLogin,
  useCan,
  useLogout,
  useMe,
  useProviders,
} from "@/auth/session";

const ME = {
  auth: "session",
  break_glass: false,
  csrf_token: "tok123",
  email: "a@example.test",
  id: "s1",
  is_system: false,
  name: "Alice",
  tenants: [{ id: "t1", name: "acme", role: "operator" }],
};

const server = setupServer(
  http.get("*/api/v1/auth/providers", () =>
    HttpResponse.json({
      break_glass: true,
      providers: [{ id: "p1", name: "Authentik", protocol: "oidc" }],
    }),
  ),
  http.get("*/api/v1/me", () => HttpResponse.json(ME)),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  setSessionExpiredHandler(null);
});
afterAll(() => server.close());

function wrapper() {
  const qc = createQueryClient();
  const W = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return { W, qc };
}

describe("session hooks", () => {
  it("lists providers", async () => {
    const { W } = wrapper();
    const { result } = renderHook(() => useProviders(), { wrapper: W });
    await waitFor(() => expect(result.current.data?.providers[0]?.name).toBe("Authentik"));
    expect(loginUrl("a/b")).toBe("/api/v1/auth/login/a%2Fb");
  });

  it("loads /me and derives permissions per tenant", async () => {
    const { W } = wrapper();
    const { result } = renderHook(() => ({ me: useMe(), can: useCan("t1"), other: useCan("t2") }), {
      wrapper: W,
    });
    expect(result.current.can("read").allowed).toBe(false);
    await waitFor(() => expect(result.current.me.data?.name).toBe("Alice"));
    expect(result.current.can("apply").allowed).toBe(true);
    expect(result.current.can("rollback").reason).toBe("Requires the tenant-admin role");
    expect(result.current.other("read").allowed).toBe(false);
  });

  it("sends the CSRF token on writes and clears state on logout", async () => {
    let seen: string | null = null;
    server.use(
      http.post("*/api/v1/auth/logout", ({ request }) => {
        seen = request.headers.get("x-csrf-token");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { W } = wrapper();
    const { result } = renderHook(() => ({ me: useMe(), out: useLogout() }), { wrapper: W });
    await waitFor(() => expect(result.current.me.data).toBeDefined());
    server.use(http.get("*/api/v1/me", () => new HttpResponse(null, { status: 401 })));
    await act(() => result.current.out.mutateAsync());
    expect(seen).toBe("tok123");
    // The token is dropped: a second write carries no CSRF header.
    seen = "unset";
    await act(() => result.current.out.mutateAsync());
    expect(seen).toBeNull();
  });

  it("break-glass login stores the session and token", async () => {
    server.use(
      http.post("*/api/v1/auth/break-glass", () => HttpResponse.json({ ...ME, break_glass: true })),
    );
    const { W, qc } = wrapper();
    const { result } = renderHook(() => useBreakGlassLogin(), { wrapper: W });
    await act(() =>
      result.current.mutateAsync({ username: "root", password: "x", totp: "123456" }),
    );
    expect((qc.getQueryData(["me"]) as { break_glass: boolean }).break_glass).toBe(true);
  });

  it("fires the session-expired handler and does not retry 401", async () => {
    const onExpired = vi.fn();
    setSessionExpiredHandler(onExpired);
    let calls = 0;
    server.use(
      http.get("*/api/v1/me", () => {
        calls++;
        return HttpResponse.json(
          { title: "Unauthorized", status: 401, detail: "expired", code: "session_expired" },
          { status: 401, headers: { "content-type": "application/problem+json" } },
        );
      }),
    );
    const { W } = wrapper();
    const { result } = renderHook(() => useMe(), { wrapper: W });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(onExpired).toHaveBeenCalledTimes(1);
    expect(calls).toBe(1);
    expect(result.current.error?.message).toBe("expired");
  });
});
