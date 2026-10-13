// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

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
import { shortWhen } from "@/lib/format";
import { usePrincipalNames } from "@/lib/principals";

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

function Detail({ tenant, id, mine }: { tenant: string; id: string; mine: boolean }) {
  const cs = useChangeSet(tenant, id);
  const name = usePrincipalNames(tenant, [cs.data?.author]);
  if (cs.isPending) return <TableSkeleton rows={2} cols={2} />;
  if (cs.isError) return <ErrorState error={cs.error} onRetry={() => void cs.refetch()} />;
  const c = cs.data;
  const row = (label: string, value: React.ReactNode) => (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd>{value}</dd>
    </>
  );
  return (
    <aside className="rounded-lg border bg-card" aria-label={t("Change set details")}>
      <h2 className="border-b px-4 py-3 text-base font-semibold">
        {c.summary || t("(no summary)")}
      </h2>
      <div className="space-y-4 p-4 text-sm">
        <dl className="grid grid-cols-[8rem_1fr] gap-y-2">
          {row(t("Author"), name(c.author))}
          {row(t("State"), <StatePill state={c.state} />)}
          {row(t("Base revision"), `#${c.base_revision}`)}
          {row(t("Created"), shortWhen(c.created_at))}
          {c.approver && row(t("Approver"), c.approver)}
          {c.error && row(t("Error"), <span className="text-status-down">{c.error}</span>)}
        </dl>
        {c.reject_comment && (
          <p className="rounded-md bg-muted p-3">
            <span className="font-medium">{t("Rejection comment:")}</span> {c.reject_comment}
          </p>
        )}
        <div className="space-y-2">
          <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("Ops · {n}", { n: c.ops.length })}
          </div>
          {c.ops.map((o, i) => (
            <div key={i} className="rounded-md border p-3">
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded bg-muted px-1.5 py-0.5 text-xs">{o.action}</span>
                <span className="font-medium">
                  {o.object_type.replace("_", " ")}{" "}
                  {String((o.body as { name?: string } | null)?.name ?? o.object_id.slice(0, 8))}
                </span>
                {o.expected_version != null && (
                  <span className="ml-auto font-mono text-xs text-muted-foreground">
                    {t("expects v{n}", { n: o.expected_version })}
                  </span>
                )}
              </div>
              {o.body && o.action === "update" && (
                <p className="mt-1 font-mono text-xs text-muted-foreground">
                  {Object.keys(o.body).join(", ")}
                </p>
              )}
            </div>
          ))}
        </div>
        {!mine && (
          <p className="text-xs text-muted-foreground">
            {t("This is someone else's draft; it does not affect your view.")}
          </p>
        )}
      </div>
    </aside>
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
  const name = usePrincipalNames(
    tenant,
    (q.data ?? []).map((c) => c.author),
  );
  const selected = q.data?.find((c) => c.id === openId) ?? q.data?.[0];

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
      <div role="tablist" className="flex gap-1 overflow-x-auto border-b">
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
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
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
                  <li key={c.id} className={cn(selected?.id === c.id && "bg-accent/40")}>
                    <div className="flex items-center gap-4 px-4 py-3 text-[13px]">
                      <button
                        type="button"
                        className="flex-1 text-left font-medium text-primary hover:underline"
                        aria-current={selected?.id === c.id}
                        onClick={() => setOpenId(c.id)}
                      >
                        {c.summary || t("(no summary)")}
                        {mine && (
                          <span className="ml-2 rounded-full bg-muted px-2 py-0.5 text-xs font-normal text-muted-foreground">
                            {t("yours")}
                          </span>
                        )}
                      </button>
                      <span className="w-44 truncate text-muted-foreground">{name(c.author)}</span>
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
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        {selected && (
          <Detail
            tenant={tenant}
            id={selected.id}
            mine={pickOwnDraft([selected], me) !== undefined}
          />
        )}
      </div>
    </div>
  );
}
