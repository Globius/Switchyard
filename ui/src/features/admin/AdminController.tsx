// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Admin > Controller (system-admin): where the controller answers (FQDN, VIP) and which certificate
 * it shows. A change applies at once, then waits two minutes for a confirmation from the NEW
 * address, which proves the name, address and certificate work; unconfirmed changes revert on
 * their own (the worker does it, not this page). ADR 0081.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Clock, ExternalLink } from "lucide-react";
import { useEffect, useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { ExpiryPill, expiryStatus } from "@/components/sy/StatusPill";
import { ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { ControllerMembers } from "./ControllerMembers";
import { t } from "@/lib/i18n";
import { isIp } from "@/lib/ip";
import { shortWhen } from "@/lib/format";
import { cn } from "@/lib/utils";

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

export type ControllerState = components["schemas"]["ControllerRead"];

function useController() {
  return useQuery({
    queryKey: ["admin", "controller"],
    refetchInterval: (q) => (q.state.data?.pending ? 4000 : false),
    queryFn: async () => unwrap(await api.GET("/api/v1/system/controller")),
  });
}

function useManagementCerts(enabled: boolean) {
  return useQuery({
    queryKey: ["admin", "controller", "certs"],
    enabled,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/certificates", {
          params: { path: { t: "management" }, query: { limit: 200 } },
        }),
      ),
  });
}

/** mm:ss from a number of seconds. */
export function clock(s: number): string {
  const n = Math.max(0, Math.floor(s));
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
}

/** True when `host` (a name) is covered by one of the certificate's SANs, including wildcards. */
export function coversName(sans: string[], host: string): boolean {
  const h = host.toLowerCase();
  return sans.some((s) => {
    const x = s.toLowerCase();
    return (
      x === h ||
      (x.startsWith("*.") && h.endsWith(x.slice(1)) && h.split(".").length === x.split(".").length)
    );
  });
}

/**
 * The countdown. Mount it with `key={secondsLeft}` so every refetch from the server re-syncs it;
 * between refetches it counts down locally.
 */
function Countdown({ secondsLeft, total }: { secondsLeft: number; total: number }) {
  const [deadline] = useState(() => Date.now() + secondsLeft * 1000);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const left = Math.max(0, Math.round((deadline - now) / 1000));
  const pct = total > 0 ? Math.min(100, Math.max(0, (left / total) * 100)) : 0;
  // Announce politely about once every ten seconds, not every tick.
  const announce =
    left > 0 && left % 10 === 0 ? t("Reverts automatically in {time}", { time: clock(left) }) : "";
  return (
    <div className="space-y-1">
      <div className="text-xs text-muted-foreground">{t("Reverts automatically in")}</div>
      <div
        role="timer"
        aria-label={t("Time left to confirm")}
        className="font-mono text-4xl font-semibold"
      >
        {clock(left)}
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded bg-muted" aria-hidden>
        <div className="h-full bg-status-degraded transition-all" style={{ width: `${pct}%` }} />
      </div>
      <span className="sr-only" aria-live="polite">
        {announce}
      </span>
    </div>
  );
}

export function AdminController() {
  const qc = useQueryClient();
  const q = useController();
  const s = q.data;
  const certs = useManagementCerts(s?.managed === true);
  const [fqdn, setFqdn] = useState<string | null>(null);
  const [vip, setVip] = useState<string | null>(null);
  const [cert, setCert] = useState<string | null>(null);
  const [confirmApply, setConfirmApply] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);

  const f = fqdn ?? s?.fqdn ?? "";
  const v = vip ?? s?.listen?.vip ?? "";
  const certName = cert ?? s?.certificate?.name ?? "";
  const pending = s?.pending ?? null;
  const waiting = pending?.state === "waiting" ? pending : null;
  const readOnly = !!pending;
  const dirty = s
    ? f !== (s.fqdn ?? "") ||
      v !== (s.listen?.vip ?? "") ||
      certName !== (s.certificate?.name ?? "")
    : false;
  const reachedHost = (() => {
    try {
      return s?.reached_at ? new URL(s.reached_at).hostname : null;
    } catch {
      return null;
    }
  })();
  const newFqdn = pending?.after.fqdn ?? null;
  const onNewAddress = newFqdn ? globalThis.location?.hostname === newFqdn : false;

  const apply = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.PUT("/api/v1/system/controller/pending", {
          body: {
            fqdn: f,
            reset_to_default: false,
            ...(s?.managed
              ? {
                  vip: v && v !== s.listen?.vip ? v : null,
                  certificate: certName && certName !== s.certificate?.name ? certName : null,
                }
              : {}),
          },
        }),
      ),
    onSuccess: () => {
      setConfirmApply(false);
      void qc.invalidateQueries({ queryKey: ["admin", "controller"] });
    },
    onError: (e) => {
      setConfirmApply(false);
      setProblem(e);
    },
  });
  const act = useMutation({
    mutationFn: async (kind: "confirm" | "revert") =>
      unwrap(
        kind === "confirm"
          ? await api.POST("/api/v1/system/controller/confirm")
          : await api.POST("/api/v1/system/controller/revert"),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["admin", "controller"] }),
    onError: setProblem,
  });

  const names = (certs.data?.items ?? []).filter((c) => !f || coversName(c.sans, f));
  const selected = (certs.data?.items ?? []).find((c) => c.name === certName);
  const certCovers = !s?.managed || !selected || coversName(selected.sans, f);
  const vipBad = v !== "" && !isIp(v);
  const fqdnChanged = f !== (s?.fqdn ?? "") && f !== "";
  const missing = fqdnChanged ? (s?.redirect_urls ?? []) : [];
  const fieldErrors: Record<string, string> = {};
  if (problem instanceof ApiError && problem.status === 422)
    for (const e of problem.problem?.errors ?? [])
      fieldErrors[e.path.split(".")[0] ?? e.path] = e.message;

  if (q.isPending) return <TableSkeleton rows={4} cols={2} />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const d = s!;
  const ex = d.certificate ? expiryStatus(d.certificate.not_after) : null;

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">{t("Controller")}</h1>
        <p className="text-sm text-muted-foreground">
          {t(
            "Where the controller answers and which certificate it shows. Changes apply at once, with a confirmation window and an automatic revert.",
          )}
        </p>
      </div>

      {problem != null && Object.keys(fieldErrors).length === 0 && <ErrorState error={problem} />}

      {pending && (
        <section className="overflow-hidden rounded-lg border border-status-degraded/60 bg-card">
          <header className="flex items-center gap-2 border-b bg-status-degraded-bg px-4 py-3">
            <Clock className="size-4" aria-hidden />
            <h2 className="text-base font-semibold">
              {waiting ? t("Waiting for you to confirm") : t("Applying the change")}
            </h2>
            <span className="text-xs text-status-degraded">{t("Pending")}</span>
          </header>
          <div className="space-y-4 p-4">
            <div className="grid gap-4 md:grid-cols-[16rem_1fr]">
              <div>
                {waiting ? (
                  <Countdown
                    key={waiting.seconds_left ?? 0}
                    secondsLeft={waiting.seconds_left ?? 0}
                    total={waiting.window_s}
                  />
                ) : (
                  <p role="status" className="text-sm text-muted-foreground">
                    {t(
                      "The change is being applied to the management tenant. The window starts when it is live.",
                    )}
                  </p>
                )}
                <div className="mt-1 text-xs text-muted-foreground">
                  {t("of {time}", { time: clock(pending.window_s) })} ·{" "}
                  {t("proposed {when}", { when: shortWhen(pending.proposed_at) })}{" "}
                  {t("by {who}", { who: pending.proposed_by })}
                </div>
              </div>
              <p className="text-sm">
                {onNewAddress
                  ? t(
                      "You are on the new address. Keeping the settings confirms that the name, addresses and certificate work. If nobody confirms in time, the controller goes back to the previous settings on its own.",
                    )
                  : t(
                      "You are still on the old address. Confirming must happen from the new one, so it proves the name, addresses and certificate work. If the link doesn't open, revert now or wait. If nobody confirms in time, the controller goes back to the previous FQDN, addresses and certificate on its own.",
                    )}
              </p>
            </div>
            <div className="flex flex-wrap gap-2">
              {!waiting ? null : onNewAddress || !newFqdn || newFqdn === pending.before.fqdn ? (
                <Button disabled={act.isPending} onClick={() => act.mutate("confirm")}>
                  {t("Keep the new settings")}
                </Button>
              ) : (
                <Button asChild>
                  <a
                    href={`https://${newFqdn}/admin/controller`}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    <ExternalLink className="size-4" aria-hidden />{" "}
                    {t("Open https://{host} to confirm", { host: newFqdn })}
                  </a>
                </Button>
              )}
              <Button
                variant="outline"
                disabled={act.isPending}
                onClick={() => act.mutate("revert")}
              >
                {t("Revert now")}
              </Button>
            </div>
            <table className="w-full overflow-hidden rounded-lg border text-[13px]">
              <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">{t("Setting")}</th>
                  <th className="px-3 py-2 font-medium">{t("Before")}</th>
                  <th className="px-3 py-2 font-medium">{t("Now (pending)")}</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {(
                  [
                    ["FQDN", pending.before.fqdn, pending.after.fqdn],
                    ["Listen address", pending.before.vip, pending.after.vip],
                    ["Certificate", pending.before.certificate, pending.after.certificate],
                  ] as const
                ).map(([label, before, after]) => (
                  <tr key={label}>
                    <td className="px-3 py-2">{t(label)}</td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {before === after ? t("unchanged") : (before ?? "—")}
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {before === after ? "" : (after ?? "—")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <section className="rounded-lg border bg-card p-4">
        <dl className="grid grid-cols-[9rem_1fr] gap-y-2 text-sm">
          <dt className="text-muted-foreground">{t("Serving now")}</dt>
          <dd className="font-mono">{d.public_url ?? (d.fqdn ? `https://${d.fqdn}` : "—")}</dd>
          <dt className="text-muted-foreground">{t("Listen address")}</dt>
          <dd className="font-mono text-xs break-all">
            {d.listen ? `${d.listen.vip} · ${d.listen.ports.map((p) => p.port).join(", ")}` : "—"}
          </dd>
          <dt className="text-muted-foreground">{t("Certificate")}</dt>
          <dd className="flex flex-wrap items-center gap-2">
            {d.certificate ? (
              <>
                {d.certificate.name}
                {ex && <ExpiryPill ex={ex} />}
              </>
            ) : (
              "—"
            )}
          </dd>
        </dl>
      </section>

      {!d.managed && (
        <p role="note" className="rounded-md bg-muted p-3 text-sm">
          {d.management_tenant
            ? t(
                "The controller service has not been set up, so only the FQDN (the public URL) can be changed. The listen address and certificate are managed outside Switchyard.",
              )
            : t(
                "There is no management tenant here, so only the FQDN (the public URL) can be changed. The listen address and certificate are managed outside Switchyard.",
              )}
        </p>
      )}
      {d.last_result && !pending && (
        <p role="status" className="rounded-md bg-muted p-3 text-sm">
          {d.last_result.kind === "confirmed"
            ? t("The last change was confirmed {when}.", { when: shortWhen(d.last_result.at) })
            : d.last_result.kind === "reverted"
              ? t("The last change was reverted {when}{why}.", {
                  when: shortWhen(d.last_result.at),
                  why: d.last_result.reason ? `: ${d.last_result.reason}` : "",
                })
              : t("The last change failed {when}{why}.", {
                  when: shortWhen(d.last_result.at),
                  why: d.last_result.reason ? `: ${d.last_result.reason}` : "",
                })}
        </p>
      )}

      <section className="space-y-4 rounded-lg border bg-card p-4">
        <h2 className="text-base font-semibold">{t("Address")}</h2>
        <label className="block space-y-1.5 text-sm font-medium">
          {t("FQDN")}
          <input
            aria-label={t("FQDN")}
            className={cn(INPUT, "font-mono")}
            disabled={readOnly}
            value={f}
            onChange={(e) => setFqdn(e.target.value)}
          />
          <span className="block text-xs font-normal text-muted-foreground">
            {t(
              "The name people and API clients use. It must resolve to the listen address below. Sign-in redirect URLs at your identity providers must use it too.",
            )}
          </span>
          {!d.managed && reachedHost && reachedHost !== f && !readOnly && (
            <Button type="button" size="sm" variant="outline" onClick={() => setFqdn(reachedHost)}>
              {t("Use {host}, the address you are on", { host: reachedHost })}
            </Button>
          )}
          {!d.managed && (
            <span className="block text-xs font-normal text-muted-foreground">
              {t("While unmanaged, an IP address is allowed (IPv6 in square brackets).")}
            </span>
          )}
          {fieldErrors.fqdn && (
            <span role="alert" className="block text-xs font-normal text-status-down">
              {fieldErrors.fqdn}
            </span>
          )}
        </label>
        {missing.length > 0 && !readOnly && (
          <div
            role="note"
            className="flex gap-2 rounded-lg border border-status-degraded/50 bg-status-degraded-bg p-3 text-sm"
          >
            <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
            <div className="space-y-1">
              <p>
                <strong>{t("Update the redirect URLs first.")}</strong>{" "}
                {t(
                  "Add the new callback URL at each identity provider before you apply, or nobody can sign in at the new address. Break-glass works either way.",
                )}
              </p>
              <ul className="font-mono text-xs break-all">
                {missing.map((r) => (
                  <li key={r.provider}>
                    {r.provider} ({r.protocol}): {r.redirect_url}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
        {d.managed && (
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Listen address (VIP)")}
            <input
              aria-label={t("Listen address (VIP)")}
              className={cn(INPUT, "font-mono")}
              disabled={readOnly}
              placeholder="10.40.0.10 or 2001:db8:40::10"
              value={v}
              onChange={(e) => setVip(e.target.value)}
            />
            <span className="block text-xs font-normal text-muted-foreground">
              {t(
                "The address the management SE group answers on for the controller. IPv4 or IPv6.",
              )}
            </span>
            {(vipBad || fieldErrors.vip) && (
              <span role="alert" className="block text-xs font-normal text-status-down">
                {fieldErrors.vip ?? t("Enter an IPv4 or IPv6 address.")}
              </span>
            )}
          </label>
        )}
      </section>

      {d.managed && (
        <section className="space-y-3 rounded-lg border bg-card p-4">
          <h2 className="text-base font-semibold">{t("TLS certificate")}</h2>
          <div className="space-y-1.5">
            <div className="text-sm font-medium">{t("Certificate")}</div>
            <Combobox
              aria-label={t("Certificate")}
              disabled={readOnly}
              value={certName}
              onChange={setCert}
              placeholder={t("Choose a certificate")}
              options={names.map((c) => ({ value: c.name, label: c.name }))}
            />
            <p className="text-xs text-muted-foreground">
              {t(
                "Lists the management tenant's certificates that cover the FQDN. Upload one, or request one from an ACME provider in advance, under Certificates in the management tenant, so the switch doesn't wait for the CA. It goes live only with the rest of this change.",
              )}
            </p>
            {!certCovers && (
              <p role="alert" className="text-xs text-status-down">
                {t("This certificate does not cover {name}.", { name: f })}
              </p>
            )}
            {fieldErrors.certificate && (
              <p role="alert" className="text-xs text-status-down">
                {fieldErrors.certificate}
              </p>
            )}
          </div>
        </section>
      )}

      <div className="flex flex-wrap items-center justify-end gap-3">
        <span className="text-xs text-muted-foreground">
          {t(
            "Applying switches the address and certificate together, then waits for you to confirm from the new address.",
          )}
        </span>
        <Button
          variant="ghost"
          disabled={!dirty || readOnly}
          onClick={() => {
            setFqdn(null);
            setVip(null);
            setCert(null);
            setProblem(null);
          }}
        >
          {t("Revert changes")}
        </Button>
        <Button
          disabled={!dirty || readOnly || vipBad || !certCovers || apply.isPending}
          onClick={() => setConfirmApply(true)}
        >
          {t("Apply and confirm")}
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        {t("Only system-admins see this page. Every apply, confirm and revert is audited.")}{" "}
        {d.break_glass}
      </p>

      <ControllerMembers />

      <ConfirmDialog
        open={confirmApply}
        onOpenChange={setConfirmApply}
        title={t("Apply controller settings?")}
        confirmLabel={t("Apply and confirm")}
        busy={apply.isPending}
        onConfirm={() => apply.mutate()}
      >
        <p>
          {t(
            "The settings switch at once. You then have two minutes to open the new address and keep them; otherwise the controller goes back by itself.",
          )}
        </p>
        <ul className="font-mono text-xs">
          {f !== d.fqdn && (
            <li>
              FQDN: {d.fqdn} → {f}
            </li>
          )}
          {d.managed && v !== (d.listen?.vip ?? "") && (
            <li>
              {t("Listen address")}: {d.listen?.vip ?? "—"} → {v || "—"}
            </li>
          )}
          {d.managed && certName !== (d.certificate?.name ?? "") && (
            <li>
              {t("Certificate")}: {d.certificate?.name ?? "—"} → {certName || "—"}
            </li>
          )}
        </ul>
      </ConfirmDialog>
    </div>
  );
}
