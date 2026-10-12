// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Renewal history of one certificate: the deadline, "Retry renewal now" and every attempt with the CA's error. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { StatusPill } from "@/components/sy/StatusPill";
import { ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Tip } from "@/components/ui/tooltip";
import { shortDate, shortWhen } from "@/lib/format";
import { t } from "@/lib/i18n";

export type RenewalStatus = components["schemas"]["RenewalStatusRead"];

export function useRenewalStatus(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "certificate-renewals"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/certificate-renewals", {
          params: { path: { t: tenant } },
        }),
      ),
  });
}

/** What the Renewal column says for one certificate, and a second line under it. */
export function renewalSummary(
  s: RenewalStatus,
  now = new Date(),
): { failed: boolean; line: string; sub: string } {
  if (s.renew_policy === "manual")
    return {
      failed: false,
      line: t("Manual"),
      sub: t("Replace before {date}", { date: shortDate(s.not_after) }),
    };
  const line = t("Auto · {n} days before", { n: s.renew_before_days });
  if (s.last_ok === false)
    return {
      failed: true,
      line,
      sub: s.last_attempt_at ? shortWhen(s.last_attempt_at, now) : "",
    };
  const parts: string[] = [];
  if (s.last_success_at) parts.push(t("renewed {date}", { date: shortDate(s.last_success_at) }));
  parts.push(t("next about {date}", { date: shortDate(s.renew_due_at) }));
  return { failed: false, line, sub: parts.join(" · ") };
}

export function RenewalHistory({
  tenant,
  cert,
  canRenew,
  renewReason,
  onClose,
}: {
  tenant: string;
  cert: { id: string; name: string };
  canRenew: boolean;
  renewReason?: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const status = useRenewalStatus(tenant);
  const s = status.data?.find((x) => x.certificate_id === cert.id);
  const history = useQuery({
    queryKey: ["t", tenant, "certificates", cert.id, "renewals"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/certificates/{certificate}/renewals", {
          params: { path: { t: tenant, certificate: cert.id }, query: { limit: 50 } },
        }),
      ),
  });
  const retry = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/certificates/{certificate}/renew", {
          params: { path: { t: tenant, certificate: cert.id } },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["t", tenant] }),
  });
  const [now] = useState(() => Date.now());
  const overdue = s ? new Date(s.renew_due_at).getTime() < now : false;
  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={t("Renewal history of {name}", { name: cert.name })}
      footer={
        <>
          <Tip label={renewReason}>
            <Button disabled={!canRenew || retry.isPending} onClick={() => retry.mutate()}>
              {t("Retry renewal now")}
            </Button>
          </Tip>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Close")}
          </Button>
        </>
      }
    >
      {retry.error && <ErrorState error={retry.error} />}
      {retry.isSuccess && (
        <p role="status" className="rounded-md bg-muted p-3 text-sm">
          {t("Renewal started as a job. Follow it on the Jobs screen.")}
        </p>
      )}
      {s && (
        <div role="note" className="space-y-1 rounded-lg border p-3 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            {s.last_ok === false ? (
              <StatusPill status="down">{t("Renewal failed")}</StatusPill>
            ) : (
              <StatusPill status={overdue ? "degraded" : "neutral"}>
                {s.renew_policy === "auto"
                  ? t("Auto · {n} days before", { n: s.renew_before_days })
                  : t("Manual")}
              </StatusPill>
            )}
            <span className="text-muted-foreground">
              {t("Expires {date}", { date: shortDate(s.not_after) })}
              {s.renew_policy === "auto" &&
                ` · ${t("renewal starts {date}", { date: shortDate(s.renew_due_at) })}`}
            </span>
          </div>
          {s.last_ok === false && s.last_error && (
            <p className="font-mono text-xs break-words">{s.last_error}</p>
          )}
          {s.failures_in_a_row > 1 && (
            <p className="text-xs text-muted-foreground">
              {t("{n} failed attempts in a row.", { n: s.failures_in_a_row })}
            </p>
          )}
        </div>
      )}
      {history.isPending ? (
        <TableSkeleton rows={3} cols={4} />
      ) : history.isError ? (
        <ErrorState error={history.error} onRetry={() => void history.refetch()} />
      ) : history.data.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("No renewal attempts yet.")}</p>
      ) : (
        <table className="w-full text-[13px]">
          <thead className="text-left text-xs text-muted-foreground">
            <tr>
              {["When", "Trigger", "Provider", "Result"].map((h) => (
                <th key={h} className="px-2 py-2 font-medium">
                  {t(h)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y">
            {history.data.map((r) => (
              <tr key={r.id} className="align-top">
                <td className="px-2 py-2 whitespace-nowrap">{shortWhen(r.started_at)}</td>
                <td className="px-2 py-2">{r.trigger === "auto" ? t("Automatic") : t("Manual")}</td>
                <td className="px-2 py-2">
                  {r.acme_provider ?? "—"}
                  {r.challenge && (
                    <span className="block text-xs text-muted-foreground">{r.challenge}</span>
                  )}
                </td>
                <td className="px-2 py-2">
                  {r.ok ? (
                    <>
                      <StatusPill status="healthy">{t("Issued")}</StatusPill>
                      {r.not_after && (
                        <span className="block text-xs text-muted-foreground">
                          {t("until {date}", { date: shortDate(r.not_after) })}
                        </span>
                      )}
                      {r.change_set_id && (
                        <Link
                          to="/t/$tenant/review/$cs"
                          params={{ tenant, cs: r.change_set_id }}
                          className="block text-xs text-primary hover:underline"
                        >
                          {t("Change set")}
                        </Link>
                      )}
                    </>
                  ) : (
                    <>
                      <StatusPill status="down">{t("Failed")}</StatusPill>
                      {r.error && (
                        <span className="block font-mono text-xs break-words text-muted-foreground">
                          {r.error}
                        </span>
                      )}
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Sheet>
  );
}
