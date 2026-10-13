/** Admin: the quota requests of every tenant, to approve or deny (ADR 0199). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { RequestState } from "./Portal";
import { resourceLabel, type QuotaRequest } from "./resources";

type StateFilter = "pending" | "approved" | "denied" | "withdrawn";
const STATES: StateFilter[] = ["pending", "approved", "denied", "withdrawn"];
const STATE_LABEL: Record<StateFilter, string> = {
  pending: "Waiting",
  approved: "Approved",
  denied: "Denied",
  withdrawn: "Withdrawn",
};
const INPUT =
  "w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function useQuotaQueue(state: StateFilter) {
  return useQuery({
    queryKey: ["admin", "quota-requests", state],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/quota-requests", { params: { query: { state, limit: 200 } } })),
  });
}

/** How many requests wait, for the badge in the admin nav. */
export function usePendingCount() {
  const q = useQuotaQueue("pending");
  return q.data?.length ?? 0;
}

function DecisionSheet({
  request,
  mode,
  onClose,
}: {
  request: QuotaRequest;
  mode: "approve" | "deny";
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [granted, setGranted] = useState(String(request.requested_limit));
  const [note, setNote] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<unknown>(null);
  const decide = useMutation({
    mutationFn: async () => {
      const params = { path: { request: request.id } };
      return mode === "approve"
        ? unwrap(
            await api.POST("/api/v1/quota-requests/{request}/approve", {
              params,
              body: { granted_limit: Number(granted), note: note.trim() || null },
            }),
          )
        : unwrap(
            await api.POST("/api/v1/quota-requests/{request}/deny", {
              params,
              body: { note: note.trim() || null },
            }),
          );
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "quota-requests"] });
      void qc.invalidateQueries({ queryKey: ["admin", "tenants"] });
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
      title={
        mode === "approve"
          ? t("Approve {resource} for {tenant}", {
              resource: resourceLabel(request.resource).toLowerCase(),
              tenant: request.tenant,
            })
          : t("Deny {resource} for {tenant}", {
              resource: resourceLabel(request.resource).toLowerCase(),
              tenant: request.tenant,
            })
      }
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            variant={mode === "approve" ? "default" : "outline"}
            disabled={decide.isPending || (mode === "deny" && !note.trim())}
            onClick={() => decide.mutate()}
          >
            {mode === "approve" ? t("Approve") : t("Deny")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      <p className="text-sm text-muted-foreground">
        {t("{who} asks to go from {from} to {to}: “{reason}”", {
          who: request.requested_by,
          from: request.current_limit,
          to: request.requested_limit,
          reason: request.reason,
        })}
      </p>
      {mode === "approve" && (
        <label className="block space-y-1.5 text-sm font-medium">
          {t("Limit to grant")}
          <input
            aria-label={t("Limit to grant")}
            type="number"
            min={request.current_limit}
            max={request.requested_limit}
            className={cn(INPUT, "h-9")}
            value={granted}
            onChange={(e) => setGranted(e.target.value)}
          />
          <span className="block text-xs font-normal text-muted-foreground">
            {t("Between {from} and {to}. Less than asked is fine.", {
              from: request.current_limit,
              to: request.requested_limit,
            })}
          </span>
          {errors.granted_limit && (
            <span role="alert" className="block text-xs font-normal text-status-down">
              {errors.granted_limit}
            </span>
          )}
        </label>
      )}
      <label className="block space-y-1.5 text-sm font-medium">
        {mode === "approve" ? t("Note for the tenant (optional)") : t("Why is it denied?")}
        <textarea
          aria-label={
            mode === "approve" ? t("Note for the tenant (optional)") : t("Why is it denied?")
          }
          rows={3}
          maxLength={500}
          className={cn(INPUT, "py-2")}
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        {errors.note && (
          <span role="alert" className="block text-xs font-normal text-status-down">
            {errors.note}
          </span>
        )}
      </label>
    </Sheet>
  );
}

export function QuotaQueue() {
  const [state, setState] = useState<StateFilter>("pending");
  const [deciding, setDeciding] = useState<{
    request: QuotaRequest;
    mode: "approve" | "deny";
  } | null>(null);
  const list = useQuotaQueue(state);
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">{t("Quota requests")}</h1>
        <p className="text-sm text-muted-foreground">
          {t("Tenants ask for higher limits here. Approving raises the quota straight away.")}
        </p>
      </div>
      <div role="group" aria-label={t("State")} className="flex gap-2">
        {STATES.map((s) => (
          <Button
            key={s}
            size="sm"
            variant={state === s ? "default" : "outline"}
            aria-pressed={state === s}
            onClick={() => setState(s)}
          >
            {t(STATE_LABEL[s])}
          </Button>
        ))}
      </div>
      <div className="overflow-x-auto rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={6} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : list.data.length === 0 ? (
          <EmptyState
            title={state === "pending" ? t("Nothing is waiting") : t("No requests in this state")}
            body={t("Requests from tenant administrators show up here.")}
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["Tenant", "What", "Change", "Why", "Asked by", "State", ""].map((h, i) => (
                  <th key={i} className="px-4 py-2.5 font-medium">
                    {h && t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {list.data.map((r) => (
                <tr key={r.id}>
                  <td className="px-4 py-3 font-medium">{r.tenant}</td>
                  <td className="px-4 py-3">{t(resourceLabel(r.resource))}</td>
                  <td className="px-4 py-3 font-mono">
                    {r.current_limit} → {r.requested_limit}
                  </td>
                  <td className="max-w-72 px-4 py-3 text-muted-foreground">{r.reason}</td>
                  <td className="px-4 py-3">
                    {r.requested_by}
                    <div className="text-xs text-muted-foreground">
                      {new Date(r.requested_at).toLocaleDateString()}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <RequestState state={r.state} />
                  </td>
                  <td className="px-4 py-3 text-right whitespace-nowrap">
                    {r.state === "pending" && (
                      <>
                        <Button
                          size="sm"
                          aria-label={t("Approve {tenant} {resource}", {
                            tenant: r.tenant,
                            resource: resourceLabel(r.resource),
                          })}
                          onClick={() => setDeciding({ request: r, mode: "approve" })}
                        >
                          {t("Approve")}
                        </Button>{" "}
                        <Button
                          size="sm"
                          variant="outline"
                          aria-label={t("Deny {tenant} {resource}", {
                            tenant: r.tenant,
                            resource: resourceLabel(r.resource),
                          })}
                          onClick={() => setDeciding({ request: r, mode: "deny" })}
                        >
                          {t("Deny")}
                        </Button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {deciding && (
        <DecisionSheet
          request={deciding.request}
          mode={deciding.mode}
          onClose={() => setDeciding(null)}
        />
      )}
    </div>
  );
}
