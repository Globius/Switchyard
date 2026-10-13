// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Shown over the page when the session ends. The page underneath stays mounted, so unsaved form
 * input survives; signing in again (another tab, or break-glass here) refreshes the data and closes it.
 */
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { AlertDialog } from "radix-ui";
import { ApiError, api, setCsrfToken } from "@/api/client";
import { clearSessionExpired, useSessionExpired } from "@/auth/expired";
import { keys, loginUrl, useBreakGlassLogin, useProviders } from "@/auth/session";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function SessionExpiredModal() {
  const open = useSessionExpired();
  if (!open) return null;
  return <Body />;
}

function Body() {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const providers = useProviders();
  const glass = useBreakGlassLogin();
  const [form, setForm] = useState({ username: "", password: "", totp: "" });
  const [checking, setChecking] = useState(false);

  /** Is there a live session again (for example after signing in in another tab)? */
  async function check(): Promise<boolean> {
    setChecking(true);
    try {
      const { data, response } = await api.GET("/api/v1/me");
      if (response.ok && data) {
        setCsrfToken(data.csrf_token);
        qc.setQueryData(keys.me, data);
        done();
        return true;
      }
      return false;
    } finally {
      setChecking(false);
    }
  }

  function done() {
    clearSessionExpired();
    void qc.invalidateQueries();
  }

  useEffect(() => {
    const id = setInterval(() => void check(), 4000);
    const onFocus = () => void check();
    window.addEventListener("focus", onFocus);
    return () => {
      clearInterval(id);
      window.removeEventListener("focus", onFocus);
    };
    // check only closes over stable setters and the query client
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const glassError = glass.error instanceof ApiError ? glass.error : null;
  return (
    <AlertDialog.Root open>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="fixed inset-0 z-[60] bg-black/50" />
        <AlertDialog.Content className="fixed top-1/3 left-1/2 z-[60] w-[min(26rem,92vw)] -translate-x-1/2 space-y-4 rounded-xl border bg-card p-6 shadow-2xl">
          <AlertDialog.Title className="text-lg font-semibold">
            {t("Your session ended")}
          </AlertDialog.Title>
          <AlertDialog.Description className="text-sm text-muted-foreground">
            {t("Sign in again to continue. What you were typing on this page is kept.")}
          </AlertDialog.Description>
          {providers.data?.providers.map((p) => (
            <Button key={p.id} asChild variant="outline" className="w-full">
              <a href={loginUrl(p.id)} target="_blank" rel="noopener">
                {t("Continue with {name}", { name: p.name })}
              </a>
            </Button>
          ))}
          {providers.data?.break_glass && (
            <form
              className="space-y-2 border-t pt-3"
              onSubmit={(e) => {
                e.preventDefault();
                glass.mutate(form, { onSuccess: done });
              }}
            >
              <div className="text-sm font-medium">{t("Break-glass administrator")}</div>
              <input
                aria-label={t("Username")}
                autoComplete="username"
                className={INPUT}
                value={form.username}
                onChange={(e) => setForm({ ...form, username: e.target.value })}
              />
              <input
                aria-label={t("Password")}
                type="password"
                autoComplete="current-password"
                className={INPUT}
                value={form.password}
                onChange={(e) => setForm({ ...form, password: e.target.value })}
              />
              <input
                aria-label={t("Authenticator code")}
                inputMode="numeric"
                autoComplete="one-time-code"
                className={INPUT}
                value={form.totp}
                onChange={(e) => setForm({ ...form, totp: e.target.value })}
              />
              {glassError && (
                <p role="alert" className="text-xs text-status-down">
                  {glassError.problem?.detail ?? t("Sign-in failed.")}
                </p>
              )}
              <Button
                type="submit"
                className="w-full"
                disabled={!form.username || !form.password || !form.totp || glass.isPending}
              >
                {t("Sign in")}
              </Button>
            </form>
          )}
          <div className="flex items-center justify-between text-sm">
            <Button variant="ghost" size="sm" disabled={checking} onClick={() => void check()}>
              {t("I have signed in")}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                clearSessionExpired();
                void navigate({
                  to: "/login",
                  search: {
                    error: "session_ended",
                    next: window.location.pathname + window.location.search,
                  },
                });
              }}
            >
              {t("Leave this page")}
            </Button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
