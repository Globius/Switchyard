// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * The tenant portal: what the tenant may use, how much it has used, and a way to ask the platform
 * for more (ADR 0199). Everything else a tenant can do for itself already lives in its own pages;
 * this one links to them.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import { useCan } from "@/auth/session";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Tip } from "@/components/ui/tooltip";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import {
  resourceLabel,
  suggestedLimit,
  usagePercent,
  usageTone,
  type QuotaRequest,
} from "./resources";

const INPUT =
  "w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

function useTenantUsage(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "tenant-record"],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}", { params: { path: { t: tenant } } })),
  });
}

function useQuotaRequests(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "quota-requests"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/quota-requests", {
          params: { path: { t: tenant }, query: { limit: 100 } },
        }),
      ),
  });
}

/** The state of a request as a pill with an icon and a word. */
export function RequestState({ state }: { state: QuotaRequest["state"] }) {
  switch (state) {
    case "pending":
      return <StatusPill status="degraded">{t("Waiting for a decision")}</StatusPill>;
    case "approved":
      return <StatusPill status="healthy">{t("Approved")}</StatusPill>;
    case "denied":
      return <StatusPill status="down">{t("Denied")}</StatusPill>;
    default:
      return <StatusPill status="neutral">{t("Withdrawn")}</StatusPill>;
  }
}

function Bar({ used, limit }: { used: number; limit: number }) {
  const tone = usageTone(used, limit);
  return (
    <div
      role="meter"
      aria-valuemin={0}
      aria-valuemax={limit}
      aria-valuenow={used}
      aria-label={t("{used} of {limit} used", { used, limit })}
      className="h-2 w-40 overflow-hidden rounded-full bg-muted"
    >
      <div
        className={cn(
          "h-full",
          tone === "healthy" && "bg-status-healthy",
          tone === "degraded" && "bg-status-degraded",
          tone === "down" && "bg-status-down",
        )}
        style={{ width: `${usagePercent(used, limit)}%` }}
      />
    </div>
  );
}

function RequestSheet({
  tenant,
  resource,
  limit,
  onClose,
}: {
  tenant: string;
  resource: string;
  limit: number;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [wanted, setWanted] = useState(String(suggestedLimit(limit)));
  const [reason, setReason] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<unknown>(null);
  const send = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/quota-requests", {
          params: { path: { t: tenant } },
          body: { resource, requested_limit: Number(wanted), reason },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["t", tenant, "quota-requests"] });
      onClose();
    },
    onError: (e) => {
      setErrors({});
      setProblem(null);
      if (e instanceof ApiError && e.status === 422) {
        const m: Record<string, string> = {};
        for (const x of e.problem?.errors ?? []) m[x.path] = x.message;
        setErrors(m);
      } else setProblem(e);
    },
  });
  return (
    <Sheet
      open
      centered
      onOpenChange={(o) => !o && onClose()}
      title={t("Ask for more {resource}", { resource: resourceLabel(resource).toLowerCase() })}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            disabled={!Number.isInteger(Number(wanted)) || !reason.trim() || send.isPending}
            onClick={() => send.mutate()}
          >
            {t("Send request")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      <p className="text-sm text-muted-foreground">
        {t(
          "Your tenant can have {limit} now. A platform administrator reads the request and may grant less than you ask for.",
          { limit },
        )}
      </p>
      <label className="block space-y-1.5 text-sm font-medium">
        {t("New limit")}
        <input
          aria-label={t("New limit")}
          type="number"
          min={limit + 1}
          className={cn(INPUT, "h-9")}
          value={wanted}
          onChange={(e) => setWanted(e.target.value)}
        />
        {errors.requested_limit && (
          <span role="alert" className="block text-xs font-normal text-status-down">
            {errors.requested_limit}
          </span>
        )}
      </label>
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Why do you need it?")}
        <textarea
          aria-label={t("Why do you need it?")}
          rows={4}
          maxLength={500}
          className={cn(INPUT, "py-2")}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        {(errors.reason || errors.resource) && (
          <span role="alert" className="block text-xs font-normal text-status-down">
            {errors.reason ?? errors.resource}
          </span>
        )}
      </label>
    </Sheet>
  );
}

function Usage({ tenant }: { tenant: string }) {
  const rec = useTenantUsage(tenant);
  const reqs = useQuotaRequests(tenant);
  const can = useCan(tenant);
  const ask = can("request", "quota_request");
  const [asking, setAsking] = useState<{ resource: string; limit: number } | null>(null);
  if (rec.isPending) return <TableSkeleton rows={4} cols={4} />;
  if (rec.isError) return <ErrorState error={rec.error} onRetry={() => void rec.refetch()} />;
  const quotas = Object.entries(rec.data.quotas).sort(([a], [b]) => a.localeCompare(b));
  const pending = new Set(
    (reqs.data ?? []).filter((r) => r.state === "pending").map((r) => r.resource),
  );
  if (quotas.length === 0)
    return (
      <EmptyState
        title={t("No limits on this tenant")}
        body={t("Nothing here is capped, so there is nothing to ask for.")}
      />
    );
  return (
    <>
      <div className="overflow-x-auto rounded-lg border bg-card">
        <table className="w-full text-[13px]">
          <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              {["What", "Used", "", "Limit", ""].map((h, i) => (
                <th key={i} className="px-4 py-2.5 font-medium">
                  {h && t(h)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y">
            {quotas.map(([key, limit]) => {
              const used = rec.data.usage?.[key] ?? 0;
              const tone = usageTone(used, limit);
              return (
                <tr key={key}>
                  <td className="px-4 py-3 font-medium">{t(resourceLabel(key))}</td>
                  <td className="px-4 py-3 font-mono">{used}</td>
                  <td className="px-4 py-3">
                    <Bar used={used} limit={limit} />
                  </td>
                  <td className="px-4 py-3 font-mono">
                    {limit} {tone === "down" && <StatusPill status="down">{t("Full")}</StatusPill>}
                    {tone === "degraded" && (
                      <StatusPill status="degraded">{t("Nearly full")}</StatusPill>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {pending.has(key) ? (
                      <StatusPill status="degraded">{t("Request pending")}</StatusPill>
                    ) : (
                      <Tip label={ask.reason}>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={!ask.allowed}
                          aria-label={t("Ask for more {resource}", {
                            resource: resourceLabel(key).toLowerCase(),
                          })}
                          onClick={() => setAsking({ resource: key, limit })}
                        >
                          {t("Ask for more")}
                        </Button>
                      </Tip>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {asking && (
        <RequestSheet
          tenant={tenant}
          resource={asking.resource}
          limit={asking.limit}
          onClose={() => setAsking(null)}
        />
      )}
    </>
  );
}

function Requests({ tenant }: { tenant: string }) {
  const qc = useQueryClient();
  const list = useQuotaRequests(tenant);
  const can = useCan(tenant);
  const ask = can("request", "quota_request");
  const withdraw = useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/quota-requests/{request}/withdraw", {
          params: { path: { t: tenant, request: id } },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["t", tenant, "quota-requests"] }),
  });
  if (list.isPending) return <TableSkeleton rows={2} cols={4} />;
  if (list.isError) return <ErrorState error={list.error} onRetry={() => void list.refetch()} />;
  if (list.data.length === 0)
    return (
      <p className="text-sm text-muted-foreground">
        {t("No requests yet. Ask for more above when a limit is close.")}
      </p>
    );
  return (
    <div className="overflow-x-auto rounded-lg border bg-card">
      <table className="w-full text-[13px]">
        <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
          <tr>
            {["What", "Change", "Why", "State", "Answer", ""].map((h, i) => (
              <th key={i} className="px-4 py-2.5 font-medium">
                {h && t(h)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y">
          {list.data.map((r) => (
            <tr key={r.id}>
              <td className="px-4 py-3 font-medium">{t(resourceLabel(r.resource))}</td>
              <td className="px-4 py-3 font-mono">
                {r.current_limit} → {r.state === "approved" ? r.granted_limit : r.requested_limit}
              </td>
              <td className="max-w-64 px-4 py-3 text-muted-foreground">{r.reason}</td>
              <td className="px-4 py-3">
                <RequestState state={r.state} />
              </td>
              <td className="max-w-64 px-4 py-3 text-muted-foreground">
                {r.decision_note ?? (r.decided_at && r.state !== "withdrawn" ? "—" : "")}
              </td>
              <td className="px-4 py-3 text-right">
                {r.state === "pending" && (
                  <Tip label={ask.reason}>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={!ask.allowed || withdraw.isPending}
                      onClick={() => withdraw.mutate(r.id)}
                    >
                      {t("Withdraw")}
                    </Button>
                  </Tip>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const LINK = "rounded-lg border bg-card p-4 hover:bg-accent";

export function Portal({ tenant }: { tenant: string }) {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{t("Usage and requests")}</h1>
        <p className="text-sm text-muted-foreground">
          {t("What {tenant} may have, how much is in use, and how to ask for more.", { tenant })}
        </p>
      </div>

      <section aria-label={t("Get started")} className="grid gap-3 sm:grid-cols-3">
        <Link to="/t/$tenant/new" params={{ tenant }} className={LINK}>
          <div className="font-medium">{t("Publish an application")}</div>
          <div className="text-xs text-muted-foreground">
            {t("A virtual service and its pool, step by step.")}
          </div>
        </Link>
        <Link to="/t/$tenant/access" params={{ tenant }} className={LINK}>
          <div className="font-medium">{t("People and API tokens")}</div>
          <div className="text-xs text-muted-foreground">
            {t("Who can sign in, and tokens for automation.")}
          </div>
        </Link>
        <Link to="/t/$tenant/certificates" params={{ tenant }} className={LINK}>
          <div className="font-medium">{t("Certificates")}</div>
          <div className="text-xs text-muted-foreground">
            {t("Request, import and renew your own.")}
          </div>
        </Link>
      </section>

      <section aria-label={t("Limits")} className="space-y-2">
        <h2 className="text-lg font-semibold">{t("Limits")}</h2>
        <Usage tenant={tenant} />
      </section>

      <section aria-label={t("Your requests")} className="space-y-2">
        <h2 className="text-lg font-semibold">{t("Your requests")}</h2>
        <Requests tenant={tenant} />
      </section>
    </div>
  );
}
