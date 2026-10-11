import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { http, HttpResponse } from "msw";
import type { ReactNode } from "react";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { renewalLabel } from "@/features/certificates/Certificates";
import { usePrincipalNames } from "@/lib/principals";

const server = setupServer();
beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function wrap({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("principal names", () => {
  it("shows the label-derived name first, then the looked-up one", async () => {
    server.use(
      http.get("*/api/v1/tenants/acme/principals", () =>
        HttpResponse.json([
          { actor: "user:authentik:dana", kind: "user", display_name: "Dana Reyes" },
        ]),
      ),
    );
    const { result } = renderHook(() => usePrincipalNames("acme", ["user:authentik:dana"]), {
      wrapper: wrap,
    });
    expect(result.current("user:authentik:dana")).toBe("dana");
    await waitFor(() => expect(result.current("user:authentik:dana")).toBe("Dana Reyes"));
  });
});

describe("renewalLabel", () => {
  it("states the window for auto renewal", () => {
    expect(renewalLabel({ renew_policy: "auto", renew_before_days: 14 })).toBe(
      "Auto · 14 days before",
    );
    expect(renewalLabel({ renew_policy: "manual" })).toBe("Manual");
  });
});
