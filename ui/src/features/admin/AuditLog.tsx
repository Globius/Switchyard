/** Admin: the audit log across tenants, newest first, with filters, per-event diff and CSV export. */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Fragment, useState } from "react";
import { api, unwrap } from "@/api/client";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { objectLabel, principalName, shortRequestId, shortWhen } from "@/lib/format";

const SELECT =
  "h-9 rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

const RANGES = [
  { key: "24h", label: "Last 24 hours", ms: 86_400_000 },
  { key: "7d", label: "Last 7 days", ms: 7 * 86_400_000 },
  { key: "30d", label: "Last 30 days", ms: 30 * 86_400_000 },
  { key: "all", label: "All time", ms: 0 },
] as const;

/** Download URL for the server-side export with the filters on screen (up to 500 events). */
export function exportUrl(
  format: "csv" | "jsonl",
  f: { q?: string; tenant?: string; action?: string; actor?: string; sinceMs?: number },
  now = Date.now(),
): string {
  const p = new URLSearchParams({ format, limit: "500" });
  if (f.q) p.set("q", f.q);
  if (f.tenant) p.set("tenant", f.tenant);
  if (f.action) p.set("action", f.action);
  if (f.actor) p.set("actor", f.actor);
  if (f.sinceMs) p.set("since", new Date(now - f.sinceMs).toISOString());
  return `/api/v1/audit-events/export?${p}`;
}

function DiffView({ diff }: { diff: Record<string, unknown> }) {
  const lines = JSON.stringify(diff, null, 2).split("\n");
  return (
    <pre className="overflow-x-auto rounded-md bg-muted p-3 font-mono text-xs">
      {lines.map((l, i) => (
        <div key={i}>{l}</div>
      ))}
    </pre>
  );
}

export function AuditLog() {
  const [q, setQ] = useState("");
  const [tenant, setTenant] = useState("");
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [range, setRange] = useState<(typeof RANGES)[number]["key"]>("24h");
  const [open, setOpen] = useState<string | null>(null);
  const tenants = useQuery({
    queryKey: ["admin", "tenants"],
    queryFn: async () => unwrap(await api.GET("/api/v1/tenants")),
  });
  const actions = useQuery({
    queryKey: ["admin", "audit-actions"],
    staleTime: Infinity,
    queryFn: async () => unwrap(await api.GET("/api/v1/audit-actions")),
  });
  const span = RANGES.find((r) => r.key === range)!.ms;
  const list = useInfiniteQuery({
    queryKey: ["admin", "audit", { q, tenant, action, actor, range }],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/api/v1/audit-events", {
          params: {
            query: {
              q: q || undefined,
              tenant: tenant || undefined,
              action: action || undefined,
              actor: actor || undefined,
              since: span ? new Date(Date.now() - span).toISOString() : undefined,
              cursor: pageParam,
              limit: 50,
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const events = list.data?.pages.flatMap((p) => p.items) ?? [];
  const tenantName = (id: string | null | undefined) =>
    tenants.data?.find((x) => x.id === id)?.name;
  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("Audit log")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("Every write, runtime action, sign-in and approval across tenants, newest first")}
          </p>
        </div>
        <div className="flex gap-2">
          {(["csv", "jsonl"] as const).map((fmt) => (
            <Button key={fmt} asChild variant="outline" aria-disabled={events.length === 0}>
              <a
                href={exportUrl(fmt, { q, tenant, action, actor, sinceMs: span })}
                download
                onClick={(e) => events.length === 0 && e.preventDefault()}
              >
                {fmt === "csv" ? t("Export CSV") : t("Export JSON Lines")}
              </a>
            </Button>
          ))}
        </div>
      </div>
      <div className="flex flex-wrap gap-3">
        <input
          aria-label={t("Search")}
          placeholder={t("Search")}
          className={cn(SELECT, "w-56")}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select
          aria-label={t("Tenant")}
          className={SELECT}
          value={tenant}
          onChange={(e) => setTenant(e.target.value)}
        >
          <option value="">{t("All tenants")}</option>
          {tenants.data?.map((x) => (
            <option key={x.id} value={x.name}>
              {x.name}
            </option>
          ))}
        </select>
        <select
          aria-label={t("Action")}
          className={SELECT}
          value={action}
          onChange={(e) => setAction(e.target.value)}
        >
          <option value="">{t("Any action")}</option>
          {actions.data?.map((a) => (
            <option key={a.action} value={a.action}>
              {a.action}
            </option>
          ))}
        </select>
        <input
          aria-label={t("Actor")}
          placeholder={t("Actor")}
          className={cn(SELECT, "w-44")}
          value={actor}
          onChange={(e) => setActor(e.target.value)}
        />
        <select
          aria-label={t("Time range")}
          className={SELECT}
          value={range}
          onChange={(e) => setRange(e.target.value as typeof range)}
        >
          {RANGES.map((r) => (
            <option key={r.key} value={r.key}>
              {t(r.label)}
            </option>
          ))}
        </select>
      </div>
      <div className="overflow-hidden rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={6} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : events.length === 0 ? (
          <EmptyState
            title={t("No events")}
            body={t("Nothing matches those filters in this time range.")}
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                <th className="w-8" />
                {["Time", "Actor", "Action", "Object", "Request"].map((h) => (
                  <th key={h} className="px-3 py-2.5 font-medium">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {events.map((e, i) => {
                const id = `${e.at}-${i}`;
                const expanded = open === id;
                const live =
                  actions.data?.find((a) => a.action === e.action)?.category === "runtime";
                const glass =
                  /break.?glass/i.test(JSON.stringify(e.diff ?? {})) ||
                  e.actor.includes("breakglass");
                return (
                  <Fragment key={id}>
                    <tr className={cn(expanded && "bg-accent/40")}>
                      <td className="px-2 py-2.5">
                        <button
                          type="button"
                          aria-label={expanded ? t("Hide details") : t("Show details")}
                          aria-expanded={expanded}
                          onClick={() => setOpen(expanded ? null : id)}
                        >
                          {expanded ? (
                            <ChevronDown className="size-4" />
                          ) : (
                            <ChevronRight className="size-4" />
                          )}
                        </button>
                      </td>
                      <td className="px-3 py-2.5 font-mono text-xs">{shortWhen(e.at)}</td>
                      <td className="px-3 py-2.5">{principalName(e.actor)}</td>
                      <td className="px-3 py-2.5">
                        <span className="font-mono text-xs">{e.action}</span>
                        {live && (
                          <Badge tone="pending" className="ml-2">
                            {t("Live")}
                          </Badge>
                        )}
                        {glass && (
                          <Badge tone="down" className="ml-2">
                            {t("Break-glass")}
                          </Badge>
                        )}
                      </td>
                      <td className="px-3 py-2.5">
                        {[tenantName(e.tenant_id), objectLabel(e.object)]
                          .filter(Boolean)
                          .join(" · ") || "—"}
                      </td>
                      <td className="px-3 py-2.5 font-mono text-xs text-muted-foreground">
                        <span title={e.request_id ?? undefined}>
                          {shortRequestId(e.request_id)}
                        </span>
                      </td>
                    </tr>
                    {expanded && (
                      <tr className="bg-accent/20">
                        <td />
                        <td colSpan={5} className="px-3 py-3">
                          {e.diff ? (
                            <DiffView diff={e.diff} />
                          ) : (
                            <p className="text-muted-foreground">{t("No details recorded.")}</p>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      {list.hasNextPage && (
        <Button
          variant="outline"
          disabled={list.isFetchingNextPage}
          onClick={() => void list.fetchNextPage()}
        >
          {t("Load more")}
        </Button>
      )}
      <p className="text-xs text-muted-foreground">
        {t(
          "Break-glass applies bypass approval and always carry their stated reason in the details.",
        )}
      </p>
    </div>
  );
}
