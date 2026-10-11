import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import { useMe } from "@/auth/session";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { pickOwnDraft, type ChangeSetSummary } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Tab = "open" | "history";
const OPEN = ["draft", "pending_approval", "applying", "failed"] as const;
const HISTORY = ["applied", "discarded"] as const;

/** One list across the states of a tab (the API filters one state at a time). */
function useChangeSets(tenant: string, states: readonly string[]) {
  return useQuery({
    queryKey: ["t", tenant, "change-sets", { states }],
    queryFn: async () => {
      const pages = await Promise.all(
        states.map(async (state) =>
          unwrap(
            await api.GET("/api/v1/tenants/{t}/change-sets", {
              params: {
                path: { t: tenant },
                query: { state: state as ChangeSetSummary["state"], limit: 100 },
              },
            }),
          ),
        ),
      );
      return pages.flatMap((p) => p.items).sort((a, b) => b.created_at.localeCompare(a.created_at));
    },
  });
}

function useChangeSet(tenant: string, id: string | null) {
  return useQuery({
    queryKey: ["t", tenant, "change-sets", id],
    enabled: !!id,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/change-sets/{cs}", {
          params: { path: { t: tenant, cs: id! } },
        }),
      ),
  });
}

export function StatePill({ state }: { state: string }) {
  switch (state) {
    case "draft":
      return <StatusPill status="draft">{t("Draft")}</StatusPill>;
    case "pending_approval":
      return <StatusPill status="pending">{t("Awaiting approval")}</StatusPill>;
    case "applying":
      return <StatusPill status="pending">{t("Applying")}</StatusPill>;
    case "applied":
      return <StatusPill status="healthy">{t("Applied")}</StatusPill>;
    case "failed":
      return <StatusPill status="down">{t("Failed")}</StatusPill>;
    default:
      return <StatusPill status="neutral">{t("Discarded")}</StatusPill>;
  }
}

function Detail({ tenant, id }: { tenant: string; id: string }) {
  const cs = useChangeSet(tenant, id);
  if (cs.isPending) return <TableSkeleton rows={2} cols={2} />;
  if (cs.isError) return <ErrorState error={cs.error} onRetry={() => void cs.refetch()} />;
  const c = cs.data;
  return (
    <div className="space-y-3 border-t bg-muted/30 p-4 text-sm">
      <dl className="grid grid-cols-[8rem_1fr] gap-y-1">
        <dt className="text-muted-foreground">{t("Base revision")}</dt>
        <dd>#{c.base_revision}</dd>
        <dt className="text-muted-foreground">{t("Created")}</dt>
        <dd>{new Date(c.created_at).toLocaleString()}</dd>
        {c.approver && (
          <>
            <dt className="text-muted-foreground">{t("Approver")}</dt>
            <dd>{c.approver}</dd>
          </>
        )}
        {c.reject_comment && (
          <>
            <dt className="text-muted-foreground">{t("Rejection comment")}</dt>
            <dd>{c.reject_comment}</dd>
          </>
        )}
        {c.error && (
          <>
            <dt className="text-muted-foreground">{t("Error")}</dt>
            <dd className="text-status-down">{c.error}</dd>
          </>
        )}
      </dl>
      <div>
        <div className="mb-1 font-medium">{t("Ops · {n}", { n: c.ops.length })}</div>
        <ul className="divide-y rounded-md border bg-card">
          {c.ops.map((o, i) => (
            <li key={i} className="flex flex-wrap gap-x-3 px-3 py-2">
              <span className="font-mono">{o.action}</span>
              <span>{o.object_type.replace("_", " ")}</span>
              <span className="font-medium">
                {String((o.body as { name?: string } | null)?.name ?? o.object_id.slice(0, 8))}
              </span>
              {o.expected_version != null && (
                <span className="text-muted-foreground">
                  {t("expects v{n}", { n: o.expected_version })}
                </span>
              )}
              {o.body && o.action === "update" && (
                <span className="font-mono text-xs text-muted-foreground">
                  {Object.keys(o.body).join(", ")}
                </span>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/** Open drafts and applied history. Other people's drafts are listed but never change what you see elsewhere. */
export function ChangeSets({ tenant }: { tenant: string }) {
  const [tab, setTab] = useState<Tab>("open");
  const [openId, setOpenId] = useState<string | null>(null);
  const { data: me } = useMe();
  const open = useChangeSets(tenant, OPEN);
  const history = useChangeSets(tenant, HISTORY);
  const q = tab === "open" ? open : history;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">{t("Change sets")}</h1>
        <p className="text-sm text-muted-foreground">
          {t(
            "Drafts in {tenant}. Other people's drafts are listed here but never change what you see elsewhere.",
            { tenant },
          )}
        </p>
      </div>
      <div role="tablist" className="flex gap-1 border-b">
        {(["open", "history"] as const).map((k) => (
          <button
            key={k}
            role="tab"
            aria-selected={tab === k}
            onClick={() => setTab(k)}
            className={cn(
              "-mb-px border-b-2 border-transparent px-4 py-2 text-sm text-muted-foreground",
              tab === k && "border-primary font-medium text-foreground",
            )}
          >
            {k === "open" ? t("Open") : t("History")}
            {k === "open" && open.data ? ` ${open.data.length}` : ""}
          </button>
        ))}
      </div>
      <div className="rounded-lg border bg-card">
        {q.isPending ? (
          <TableSkeleton cols={5} />
        ) : q.isError ? (
          <ErrorState error={q.error} onRetry={() => void q.refetch()} />
        ) : q.data.length === 0 ? (
          <EmptyState
            title={tab === "open" ? t("No open change sets") : t("No history yet")}
            body={
              tab === "open"
                ? t("Edit anything and your draft appears here.")
                : t("Applied and discarded change sets appear here.")
            }
          />
        ) : (
          <ul className="divide-y">
            {q.data.map((c) => {
              const mine = pickOwnDraft([c], me) !== undefined;
              return (
                <li key={c.id}>
                  <div className="flex items-center gap-4 px-4 py-3 text-[13px]">
                    <button
                      type="button"
                      className="flex-1 text-left font-medium text-primary hover:underline"
                      aria-expanded={openId === c.id}
                      onClick={() => setOpenId(openId === c.id ? null : c.id)}
                    >
                      {c.summary || t("(no summary)")}
                      {mine && (
                        <span className="ml-2 text-xs text-muted-foreground">{t("yours")}</span>
                      )}
                    </button>
                    <span className="w-44 truncate text-muted-foreground">{c.author}</span>
                    <StatePill state={c.state} />
                    <span className="w-10 text-right text-muted-foreground">{c.op_count}</span>
                    <span className="w-28 text-right font-mono text-xs text-muted-foreground">
                      {c.id.slice(0, 8)}…
                    </span>
                    {(c.state === "draft" ||
                      c.state === "pending_approval" ||
                      c.state === "failed") && (
                      <Button size="sm" variant="outline" asChild>
                        <Link to="/t/$tenant/review/$cs" params={{ tenant, cs: c.id }}>
                          {mine ? t("Review") : t("View")}
                        </Link>
                      </Button>
                    )}
                  </div>
                  {openId === c.id && <Detail tenant={tenant} id={c.id} />}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
