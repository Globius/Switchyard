/** One timeline for a service: your draft, revisions, runtime actions and approvals. */
import { useQueries, useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { api, unwrap } from "@/api/client";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { useDraft } from "@/features/shell/draft";
import { principalName, shortWhen } from "@/lib/format";
import { t } from "@/lib/i18n";
import { diffObject } from "@/lib/snapshot-diff";
import { cn } from "@/lib/utils";
import { usePools, useVs } from "./detail-queries";
import {
  FILTERS,
  buildTimeline,
  changesInRevision,
  fieldLines,
  type Entry,
  type Filter,
} from "./history-model";

type Json = Record<string, unknown>;

/** Timeline times use the shared short-date formatter. */
export const stamp = shortWhen;

function DiffBlock({ lines }: { lines: { sign: "+" | "-"; text: string }[] }) {
  if (lines.length === 0) return null;
  return (
    <pre className="mt-2 overflow-x-auto rounded-md border font-mono text-xs leading-5">
      {lines.map((l, i) => (
        <div key={i} className={cn("px-3", l.sign === "+" ? "bg-diff-add" : "bg-diff-del")}>
          <span aria-hidden>{l.sign === "+" ? "+" : "−"} </span>
          <span className="sr-only">{l.sign === "+" ? t("added") : t("removed")}</span>
          {l.text}
        </div>
      ))}
    </pre>
  );
}

function useRevisionSnapshot(tenant: string, n: number | null) {
  return useQuery({
    queryKey: ["t", tenant, "revisions", n],
    enabled: n !== null && n > 0,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/revisions/{n}", {
          params: { path: { t: tenant, n: n! } },
        }),
      ),
  });
}

/** The service's own fields changed by revision `n`, from its snapshot and the one before. */
function RevisionDiff({ tenant, n, id }: { tenant: string; n: number; id: string }) {
  const after = useRevisionSnapshot(tenant, n);
  const before = useRevisionSnapshot(tenant, n - 1);
  if (!after.data) return null;
  const fields = changesInRevision(
    before.data?.snapshot as Json | undefined,
    after.data.snapshot as Json,
    id,
  );
  return <DiffBlock lines={fieldLines(fields)} />;
}

export function VsHistory({ tenant, id }: { tenant: string; id: string }) {
  const { draft } = useDraft(tenant);
  const vs = useVs(tenant, id, draft?.id);
  const live = useVs(tenant, id);
  const pools = usePools(tenant);
  const [filter, setFilter] = useState<Filter>("all");
  const [related, setRelated] = useState(true);

  const v = vs.data;
  const poolIds = useMemo(() => {
    const ids = new Set<string>();
    if (v?.default_pool_id) ids.add(v.default_pool_id);
    for (const r of v?.routes ?? []) if (r.pool_id) ids.add(r.pool_id);
    return [...ids];
  }, [v]);
  const monitorIds = useMemo(
    () => [
      ...new Set(
        (pools.data?.items ?? [])
          .filter((p) => poolIds.includes(p.id) && p.health_monitor_id)
          .map((p) => p.health_monitor_id!),
      ),
    ],
    [pools.data, poolIds],
  );
  const objects = useMemo(
    () => [
      { type: "virtual_service", id },
      ...(related
        ? [
            ...poolIds.map((x) => ({ type: "pool", id: x })),
            ...monitorIds.map((x) => ({ type: "health_monitor", id: x })),
            ...(v?.certificate_ids ?? []).map((x) => ({ type: "certificate", id: x })),
          ]
        : []),
    ],
    [id, related, poolIds, monitorIds, v?.certificate_ids],
  );

  const revisions = useQueries({
    queries: objects.map((o) => ({
      queryKey: ["t", tenant, "revisions", { object: o.id }],
      queryFn: async () =>
        unwrap(
          await api.GET("/api/v1/tenants/{t}/revisions", {
            params: { path: { t: tenant }, query: { object: o.id, limit: 50 } },
          }),
        ),
    })),
  });
  const audits = useQueries({
    queries: objects.map((o) => ({
      queryKey: ["t", tenant, "audit-events", { object: `${o.type}:${o.id}` }],
      queryFn: async () =>
        unwrap(
          await api.GET("/api/v1/tenants/{t}/audit-events", {
            params: { path: { t: tenant }, query: { object: `${o.type}:${o.id}`, limit: 50 } },
          }),
        ),
    })),
  });
  const actions = useQuery({
    queryKey: ["audit-actions"],
    staleTime: Infinity,
    queryFn: async () => unwrap(await api.GET("/api/v1/audit-actions")),
  });

  if (vs.isPending || revisions.some((q) => q.isPending) || audits.some((q) => q.isPending))
    return <TableSkeleton rows={5} cols={3} />;
  const err = [...revisions, ...audits].find((q) => q.error)?.error ?? vs.error;
  if (err)
    return (
      <ErrorState
        error={err}
        onRetry={() => {
          for (const q of [...revisions, ...audits]) void q.refetch();
        }}
      />
    );

  const revs = new Map<
    number,
    { number: number; summary: string; author: string; created_at: string }
  >();
  for (const q of revisions) for (const r of q.data?.items ?? []) revs.set(r.number, r);
  const categories = Object.fromEntries((actions.data ?? []).map((a) => [a.action, a.category]));
  const draftFields =
    draft && vs.data && live.data && vs.data.draft_status !== "unchanged"
      ? diffObject(live.data as unknown as Json, vs.data as unknown as Json)
      : [];
  const entries = buildTimeline({
    draft:
      draftFields.length > 0
        ? {
            at: new Date().toISOString(),
            title: t("{n} fields changed", { n: draftFields.length }),
            author: draft?.author ?? "",
            fields: draftFields,
          }
        : null,
    revisions: [...revs.values()],
    audit: audits.flatMap((q) => q.data?.items ?? []),
    categories,
    filter,
  });
  const top = new Set(
    [...revs.values()]
      .sort((a, b) => b.number - a.number)
      .slice(0, 8)
      .map((r) => r.number),
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-4">
        <div
          role="radiogroup"
          aria-label={t("Show")}
          className="inline-flex overflow-hidden rounded-md border"
        >
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              role="radio"
              aria-checked={filter === f.key}
              onClick={() => setFilter(f.key)}
              className={cn(
                "px-3 py-1.5 text-sm",
                filter === f.key
                  ? "bg-accent font-medium"
                  : "text-muted-foreground hover:bg-accent/50",
              )}
            >
              {t(f.label)}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={related} onChange={(e) => setRelated(e.target.checked)} />
          {t("Include its pools, monitors and certificates")}
        </label>
      </div>
      {entries.length === 0 ? (
        <EmptyState
          title={t("No history yet")}
          body={t("Changes to this service appear here once a revision applies.")}
        />
      ) : (
        <ol className="divide-y rounded-lg border bg-card">
          {entries.map((e, i) => (
            <li key={i} className="flex gap-4 px-4 py-4 text-[13px]">
              <span
                aria-hidden
                className={cn(
                  "mt-1.5 size-2.5 shrink-0 rounded-full",
                  e.kind === "draft"
                    ? "bg-draft"
                    : e.kind === "runtime"
                      ? "bg-status-healthy"
                      : "bg-muted-foreground/40",
                )}
              />
              <div className="min-w-0 flex-1">
                <EntryBody
                  entry={e}
                  tenant={tenant}
                  id={id}
                  draftId={draft?.id}
                  showDiff={e.kind === "revision" && top.has(e.number)}
                />
              </div>
            </li>
          ))}
        </ol>
      )}
      <p className="text-xs text-muted-foreground">
        {t(
          "Config changes come from revisions; runtime actions and approvals come from the audit log. To undo a change, roll back from Revisions; that opens a draft and goes through review like any other change.",
        )}
      </p>
    </div>
  );
}

function EntryBody({
  entry: e,
  tenant,
  id,
  draftId,
  showDiff,
}: {
  entry: Entry;
  tenant: string;
  id: string;
  draftId?: string;
  showDiff: boolean;
}) {
  if (e.kind === "draft")
    return (
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill status="draft">{t("In draft")}</StatusPill>
            <span className="font-medium">{e.title}</span>
            <span className="text-muted-foreground">
              {principalName(e.author)} · {t("not live yet")}
            </span>
          </div>
          <DiffBlock lines={fieldLines(e.fields)} />
        </div>
        {draftId && (
          <Button asChild variant="outline" size="sm">
            <Link to="/t/$tenant/review/$cs" params={{ tenant, cs: draftId }}>
              {t("Review draft")}
            </Link>
          </Button>
        )}
      </div>
    );
  if (e.kind === "revision")
    return (
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono font-semibold">#{e.number}</span>
            <span className="font-medium">{e.summary}</span>
          </div>
          <div className="text-xs text-muted-foreground">
            {principalName(e.author)} · {stamp(e.at)}
          </div>
          {showDiff && <RevisionDiff tenant={tenant} n={e.number} id={id} />}
        </div>
        <Button asChild variant="ghost" size="sm">
          <Link to="/t/$tenant/revisions" params={{ tenant }}>
            {t("Compare")}
          </Link>
        </Button>
      </div>
    );
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        {e.kind === "runtime" ? (
          <StatusPill status="pending">{t("Live")}</StatusPill>
        ) : (
          <StatusPill status="neutral">{t("Approval")}</StatusPill>
        )}
        <span className="font-mono text-xs font-medium">{e.action}</span>
        {e.object && <span className="text-muted-foreground">{e.object}</span>}
      </div>
      <div className="text-xs text-muted-foreground">
        {principalName(e.actor)} · {stamp(e.at)}
        {e.kind === "runtime" ? ` · ${t("runtime action, not a revision")}` : ""}
      </div>
    </div>
  );
}
