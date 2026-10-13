// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useNavigate } from "@tanstack/react-router";
import { AlertTriangle, Signpost } from "lucide-react";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { loginUrl, useBreakGlassLogin, useProviders } from "@/auth/session";
import { ErrorState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { t } from "@/lib/i18n";

export type LoginError = "provider_error" | "expired" | "no_access" | "disabled" | "session_ended";

/** Copy for each way back to this page: `/login?error=<code>` from the backend, or our own. */
export function errorCopy(code: LoginError, provider?: string, next?: string) {
  switch (code) {
    case "session_ended":
      return {
        title: t("Your session ended."),
        body: t(
          "Sessions last up to 4 hours, or 30 minutes without activity. Sign in to return to {next}.",
          { next: next ?? "/" },
        ),
      };
    case "provider_error":
      return {
        title: t("Sign-in didn't complete."),
        body: t(
          "The provider returned an error or the sign-in took too long. Try again, or use another provider.",
        ),
      };
    case "expired":
      return {
        title: t("That sign-in attempt expired."),
        body: t("It was too old or already used. Start again."),
      };
    case "no_access":
      return {
        title: t("You don't have access yet."),
        body: t(
          "You signed in, but none of your groups is mapped to a Switchyard role. Ask your tenant's admin to add your group.",
        ),
      };
    case "disabled":
      return {
        title: t("This account is disabled."),
        body: t(
          "A system-admin turned off your access to Switchyard. Contact them if you think this is a mistake.",
        ),
      };
  }
}

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

/** Where to go after signing in: the page you were on, if it is a path inside this app. */
export function safeNext(next: string | undefined): string | undefined {
  if (!next || !next.startsWith("/") || next.startsWith("//") || next.startsWith("/\\"))
    return undefined;
  if (/^\/(%5c|%2f)/i.test(next)) return undefined;
  try {
    // Control characters and encoded slashes resolve to a host here, so require the same origin.
    const u = new URL(next, window.location.origin);
    return u.origin === window.location.origin ? u.pathname + u.search + u.hash : undefined;
  } catch {
    return undefined;
  }
}

export function Login({ error, next }: { error?: LoginError; next?: string }) {
  const providers = useProviders();
  const glass = useBreakGlassLogin();
  const navigate = useNavigate();
  const [form, setForm] = useState({ username: "", password: "", totp: "" });
  const target = safeNext(next);
  const copy = error ? errorCopy(error, undefined, target) : null;

  function submit(e: React.FormEvent) {
    e.preventDefault();
    glass.mutate(form, {
      onSuccess: () => void navigate({ to: target ?? "/" }),
    });
  }
  const glassError = glass.error instanceof ApiError ? glass.error : null;

  return (
    <main className="grid min-h-screen place-items-center bg-muted/50 p-6">
      <div className="w-full max-w-sm space-y-4">
        <div className="flex items-center justify-center gap-2 text-lg font-semibold">
          <span className="grid size-9 place-items-center rounded-md bg-primary text-primary-foreground">
            <Signpost className="size-5" aria-hidden />
          </span>
          Switchyard
        </div>
        <div className="space-y-4 rounded-lg border bg-card p-7">
          <h1 className="text-xl font-semibold">{t("Sign in")}</h1>
          {copy && (
            <div
              role="alert"
              className="flex gap-3 rounded-md bg-status-degraded-bg p-3 text-sm text-foreground"
            >
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-status-degraded" />
              <p>
                <strong>{copy.title}</strong> {copy.body}
              </p>
            </div>
          )}
          {providers.isPending ? (
            <div role="status" aria-label={t("Loading")} className="space-y-3">
              <Skeleton className="h-11 w-full" />
              <Skeleton className="h-11 w-full" />
            </div>
          ) : providers.isError ? (
            <ErrorState error={providers.error} onRetry={() => void providers.refetch()} />
          ) : (
            <>
              {providers.data.providers.length === 0 && (
                <p className="text-sm text-muted-foreground">
                  {t("No identity provider is configured yet.")}
                </p>
              )}
              {providers.data.providers.map((p) => (
                <Button key={p.id} variant="outline" className="h-11 w-full" asChild>
                  <a
                    href={`${loginUrl(p.id)}${target ? `?return_to=${encodeURIComponent(target)}` : ""}`}
                  >
                    {t("Continue with {name}", { name: p.name })}
                  </a>
                </Button>
              ))}
              {providers.data.providers.length > 0 && (
                <p className="text-center text-xs text-muted-foreground">
                  {t("Your identity provider's groups decide which tenants and roles you get.")}
                </p>
              )}
              {providers.data.break_glass && (
                <form onSubmit={submit} className="space-y-3 border-t pt-4">
                  <h2 className="text-sm font-semibold">{t("Break-glass administrator")}</h2>
                  <input
                    aria-label={t("Username")}
                    placeholder={t("Username")}
                    autoComplete="username"
                    className={INPUT}
                    value={form.username}
                    onChange={(e) => setForm({ ...form, username: e.target.value })}
                  />
                  <input
                    aria-label={t("Password")}
                    placeholder={t("Password")}
                    type="password"
                    autoComplete="current-password"
                    className={INPUT}
                    value={form.password}
                    onChange={(e) => setForm({ ...form, password: e.target.value })}
                  />
                  <input
                    aria-label={t("Authenticator code")}
                    placeholder={t("Authenticator code")}
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={8}
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
                    disabled={
                      glass.isPending || !form.username || !form.password || form.totp.length < 6
                    }
                  >
                    {t("Sign in as break-glass")}
                  </Button>
                  <p className="text-xs text-muted-foreground">
                    {t("Every attempt is audited; five failures lock it for 15 minutes.")}
                  </p>
                </form>
              )}
            </>
          )}
        </div>
      </div>
    </main>
  );
}
