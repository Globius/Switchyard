// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory, createRouter, RouterProvider } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { createQueryClient } from "@/auth/session";
import { clock, coversName, type ControllerState } from "@/features/admin/AdminController";
import { ThemeProvider } from "@/lib/theme";
import { routeTree } from "@/routeTree.gen";

const FUTURE = new Date(Date.now() + 71 * 86_400_000).toISOString();
const BASE: ControllerState = {
  managed: true,
  management_tenant: true,
  reached_at: null,
  fqdn: "switchyard.example.test",
  public_url: "https://switchyard.example.test",
  listen: { vip: "10.40.0.10", ports: [{ port: 443, tls: true, https_redirect_port: null }] },
  certificate: { name: "switchyard-cert", sans: ["switchyard.example.test"], not_after: FUTURE },
  pending: null,
  last_result: null,
  break_glass: "If the new address does not work: syctl controller revert.",
  redirect_urls: [
    {
      provider: "authentik",
      protocol: "oidc",
      redirect_url: "https://lb.example.test/api/v1/auth/callback/x",
    },
  ],
};
let state: ControllerState = BASE;
let put: Record<string, unknown> | null = null;
let posted: string[] = [];
const server = setupServer(
  http.get("*/api/v1/me", () =>
    HttpResponse.json({
      auth: "session",
      break_glass: false,
      csrf_token: "tok",
      email: null,
      id: "s",
      is_system: true,
      name: "Miguel",
      principal: "Miguel",
      session: null,
      tenants: [],
    }),
  ),
  http.get("*/api/v1/permissions", () => HttpResponse.json({ roles: [], permissions: [] })),
  http.get("*/api/v1/tenants", () => HttpResponse.json([])),
  http.get("*/api/v1/system/controller", () => HttpResponse.json(state)),
  http.get("*/api/v1/tenants/management/certificates", () =>
    HttpResponse.json({
      items: [
        { id: "c1", name: "lb-cert", sans: ["lb.example.test"], not_after: FUTURE },
        { id: "c2", name: "other", sans: ["other.example.test"], not_after: FUTURE },
      ],
      next_cursor: null,
    }),
  ),
  http.put("*/api/v1/system/controller/pending", async ({ request }) => {
    put = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({}, { status: 202 });
  }),
  http.post("*/api/v1/system/controller/:action", ({ params }) => {
    posted.push(String(params.action));
    return HttpResponse.json({});
  }),
);
beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  state = BASE;
  put = null;
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
            history: createMemoryHistory({ initialEntries: ["/admin/controller"] }),
          })}
        />
      </QueryClientProvider>
    </ThemeProvider>,
  );
}

describe("helpers", () => {
  it("formats the countdown and matches certificate names", () => {
    expect(clock(97)).toBe("1:37");
    expect(clock(-3)).toBe("0:00");
    expect(clock(120)).toBe("2:00");
    expect(coversName(["*.example.test"], "lb.example.test")).toBe(true);
    expect(coversName(["*.example.test"], "a.b.example.test")).toBe(false);
    expect(coversName(["lb.example.test"], "LB.example.test")).toBe(true);
    expect(coversName(["other.test"], "lb.example.test")).toBe(false);
  });
});

describe("Admin > Controller", () => {
  it("shows what is served now and applies a new FQDN with the certificate after a confirm", async () => {
    renderIt();
    expect(await screen.findByText("https://switchyard.example.test")).toBeInTheDocument();
    expect(screen.getByText(/10\.40\.0\.10 · 443/)).toBeInTheDocument();
    const fqdn = screen.getByLabelText("FQDN");
    await userEvent.clear(fqdn);
    await userEvent.type(fqdn, "lb.example.test");
    expect(screen.getByText(/Update the redirect URLs first/)).toBeInTheDocument();
    expect(screen.getByText(/authentik \(oidc\): https:\/\/lb\.example\.test/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("combobox", { name: "Certificate" }));
    const list = await screen.findByRole("listbox");
    expect(within(list).queryByText("other")).toBeNull();
    await userEvent.click(within(list).getByText("lb-cert"));
    await userEvent.click(screen.getByRole("button", { name: "Apply and confirm" }));
    const dlg = within(await screen.findByRole("alertdialog"));
    expect(dlg.getByText(/switchyard\.example\.test → lb\.example\.test/)).toBeInTheDocument();
    await userEvent.click(dlg.getByRole("button", { name: "Apply and confirm" }));
    await waitFor(() =>
      expect(put).toEqual({
        fqdn: "lb.example.test",
        reset_to_default: false,
        vip: null,
        certificate: "lb-cert",
      }),
    );
  });

  it("blocks a certificate that does not cover the new name, and a bad VIP", async () => {
    renderIt();
    await screen.findByText("https://switchyard.example.test");
    await userEvent.type(screen.getByLabelText("Listen address (VIP)"), "x");
    expect(screen.getByText("Enter an IPv4 or IPv6 address.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Apply and confirm" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Revert changes" }));
    expect(screen.getByLabelText("Listen address (VIP)")).toHaveValue("10.40.0.10");
    const fqdn = screen.getByLabelText("FQDN");
    await userEvent.clear(fqdn);
    await userEvent.type(fqdn, "lb.example.test");
    expect(screen.getByRole("button", { name: "Apply and confirm" })).toBeEnabled();
    await userEvent.type(fqdn, "x");
    await userEvent.click(screen.getByRole("combobox", { name: "Certificate" }));
    expect(await screen.findByText("No matches")).toBeInTheDocument();
  });

  it("does not warn about the certificate when the FQDN is an IP address", async () => {
    state = { ...BASE, fqdn: "10.40.0.10", public_url: "https://10.40.0.10" };
    renderIt();
    await screen.findByText("https://10.40.0.10");
    expect(screen.queryByText(/does not cover/)).not.toBeInTheDocument();
    expect(screen.getByText(/The FQDN is an IP address/)).toBeInTheDocument();
  });

  it("shows the countdown and the old-address confirmation link, and reverts now", async () => {
    state = {
      ...BASE,
      pending: {
        state: "waiting",
        window_s: 120,
        seconds_left: 97,
        proposed_by: "Miguel Rodriguez",
        proposed_at: new Date().toISOString(),
        expires_at: new Date(Date.now() + 97_000).toISOString(),
        change_set_id: null,
        revision_before: 1,
        revision_after: 2,
        before: {
          fqdn: "switchyard.example.test",
          public_url: "https://switchyard.example.test",
          vip: "10.40.0.10",
          certificate: "switchyard-cert",
        },
        after: {
          fqdn: "lb.example.test",
          public_url: "https://lb.example.test",
          vip: "10.40.0.10",
          certificate: "lb-cert",
        },
      },
    };
    renderIt();
    expect(await screen.findByText("Waiting for you to confirm")).toBeInTheDocument();
    expect(screen.getByRole("timer")).toHaveTextContent(/1:3\d|1:4\d|1:37/);
    const link = screen.getByRole("link", { name: /Open https:\/\/lb\.example\.test to confirm/ });
    expect(link).toHaveAttribute("href", "https://lb.example.test/admin/controller");
    expect(screen.getByLabelText("FQDN")).toBeDisabled();
    expect(screen.getByText("unchanged")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Revert now" }));
    await waitFor(() => expect(posted).toEqual(["revert"]));
  });

  it("explains an unmanaged controller and shows how the last change ended", async () => {
    state = {
      ...BASE,
      managed: false,
      management_tenant: false,
      listen: null,
      certificate: null,
      redirect_urls: [],
      last_result: {
        kind: "reverted",
        at: new Date().toISOString(),
        by: null,
        reason: "not confirmed in time",
      },
    };
    renderIt();
    expect(await screen.findByText(/no management tenant here/)).toBeInTheDocument();
    expect(screen.getByText(/was reverted .*: not confirmed in time/)).toBeInTheDocument();
    expect(screen.getByText(/syctl controller revert/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Listen address (VIP)")).toBeNull();
  });

  it("says the controller service is not set up and offers the address in use", async () => {
    state = {
      ...BASE,
      managed: false,
      management_tenant: true,
      reached_at: "https://[2001:db8::10]:9000",
      listen: null,
      certificate: null,
      redirect_urls: [],
    };
    renderIt();
    expect(await screen.findByText(/controller service has not been set up/)).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: /Use \[2001:db8::10\], the address you are on/ }),
    );
    expect(screen.getByLabelText("FQDN")).toHaveValue("[2001:db8::10]");
  });
});
