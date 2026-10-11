/** Tenant home: what needs attention now, from one `GET /summary` call plus three short lists. */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { api, unwrap } from "@/api/client";
import { StatusPill, expiryStatus } from "@/components/sy/StatusPill";
import { ErrorState, TableSkeleton } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ReapplyButton } from "@/features/drift/ReapplyButton";
import { useDraft } from "@/features/shell/draft";
import { ago } from "@/features/vs/member-health";
import { t } from "@/lib/i18n";
import { principalName } from "@/lib/format";

function useSummary(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "summary"],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/summary", { params: { path: { t: tenant } } })),
  });
}

function useEngineCount(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "engines"],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/engines", { params: { path: { t: tenant } } })),
  });
}

function useOpenDrafts(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "change-sets", { state: "draft", home: true }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/change-sets", {
          params: { path: { t: tenant }, query: { state: "draft", limit: 5 } },
        }),
      ),
  });
}

function useRecentRevisions(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "revisions", { home: true }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/revisions", {
          params: { path: { t: tenant }, query: { limit: 3 } },
        }),
      ),
  });
}

function Card({
  title,
  count,
  action,
  children,
}: {
  title: string;
  count?: number;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <header className="flex items-center gap-2 border-b px-4 py-3">
        <h2 className="text-base font-semibold">{title}</h2>
        {count !== undefined && count > 0 && <Badge tone="degraded">{count}</Badge>}
        <span className="ml-auto text-xs">{action}</span>
      </header>
      {children}
    </section>
  );
}

function Stat({ label, value, children }: { label: string; value: number; children: ReactNode }) {
  return (
    <div className="rounded-lg border bg-card p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 text-3xl font-semibold">{value}</div>
      <div className="mt-2 flex flex-wrap gap-1.5">{children}</div>
    </div>
  );
}

const LINK = "text-primary hover:underline";

export function Home({ tenant }: { tenant: string }) {
  const summary = useSummary(tenant);
  const engines = useEngineCount(tenant);
  const drafts = useOpenDrafts(tenant);
  const revisions = useRecentRevisions(tenant);
  const { draft } = useDraft(tenant);
  if (summary.isPending) return <TableSkeleton rows={4} cols={4} />;
  if (summary.isError)
    return <ErrorState error={summary.error} onRetry={() => void summary.refetch()} />;
  const s = summary.data;
  const total = engines.data?.length ?? 0;
  const unreachable = s.unreachable_engines.length;
  const soon = s.expiring_certificates.length;
  const week = s.expiring_certificates.filter((c) => c.days_left <= 7).length;
  const problems = s.failing_members.length + unreachable + s.drifted_engines.length + soon;
  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{tenant}</h1>
          <p className="text-sm text-muted-foreground">
            {t("Live state from engine status, observed {time}", {
              time: new Date(summary.dataUpdatedAt).toLocaleTimeString(),
            })}
          </p>
        </div>
        <Button
          variant="outline"
          disabled={problems === 0}
          onClick={() =>
            document.getElementById("failing-members")?.scrollIntoView({ behavior: "smooth" })
          }
        >
          {t("Jump to problems")}
        </Button>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <Stat label={t("Virtual services")} value={s.services.total}>
          {s.services.healthy > 0 && (
            <StatusPill status="healthy">{t("{n} healthy", { n: s.services.healthy })}</StatusPill>
          )}
          {s.services.degraded > 0 && (
            <StatusPill status="degraded">
              {t("{n} degraded", { n: s.services.degraded })}
            </StatusPill>
          )}
          {s.services.down > 0 && (
            <StatusPill status="down">{t("{n} down", { n: s.services.down })}</StatusPill>
          )}
          {s.services.disabled > 0 && (
            <StatusPill status="neutral">
              {t("{n} disabled", { n: s.services.disabled })}
            </StatusPill>
          )}
        </Stat>
        <Stat label={t("Service engines")} value={total}>
          {total > 0 && (
            <StatusPill status="healthy">
              {t("{n} reachable", { n: Math.max(total - unreachable, 0) })}
            </StatusPill>
          )}
          {s.drifted_engines.length > 0 && (
            <StatusPill status="degraded">
              {t("{n} drifted", { n: s.drifted_engines.length })}
            </StatusPill>
          )}
          {unreachable > 0 && (
            <StatusPill status="down">{t("{n} unreachable", { n: unreachable })}</StatusPill>
          )}
        </Stat>
        <Stat label={t("Certificates expiring in 30 days")} value={soon}>
          {week > 0 && <StatusPill status="down">{t("{n} within 7 days", { n: week })}</StatusPill>}
        </Stat>
        <Stat label={t("Open drafts")} value={s.open_drafts}>
          <StatusPill status="draft">{t("{n} yours", { n: draft ? 1 : 0 })}</StatusPill>
          <StatusPill status="pending">
            {t("{n} awaiting approval", { n: s.pending_approval })}
          </StatusPill>
        </Stat>
      </div>

      <div className="grid items-start gap-5 lg:grid-cols-2">
        <div className="space-y-5">
          <div id="failing-members">
            <Card title={t("Failing members")} count={s.failing_members.length}>
              {s.failing_members.length === 0 ? (
                <p className="p-4 text-sm text-muted-foreground">{t("No failing members.")}</p>
              ) : (
                <table className="w-full text-[13px]">
                  <thead className="text-left text-xs text-muted-foreground">
                    <tr>
                      {["Member", "Pool", "State", "Last check"].map((h) => (
                        <th key={h} className="px-4 py-2 font-medium">
                          {t(h)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {s.failing_members.map((m) => (
                      <tr key={`${m.engine_id}-${m.backend}-${m.server}`}>
                        <td className="px-4 py-2.5 font-mono text-xs">{m.server}</td>
                        <td className="px-4 py-2.5">{m.backend}</td>
                        <td className="px-4 py-2.5">
                          <StatusPill status="down">
                            {m.status.toUpperCase().startsWith("DOWN") ? t("Down") : m.status}
                          </StatusPill>
                        </td>
                        <td className="px-4 py-2.5 text-muted-foreground">
                          <span className="font-mono text-xs">
                            {m.check_status ?? m.last_check ?? "—"}
                          </span>
                          {m.last_change_s != null && (
                            <span className="block text-xs">
                              {ago(m.last_change_s)} · {m.engine}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Card>
          </div>
          <Card title={t("Expiring certificates")}>
            {s.expiring_certificates.length === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">
                {t("Nothing expires in the next 30 days.")}
              </p>
            ) : (
              <ul className="divide-y text-[13px]">
                {s.expiring_certificates.map((c) => {
                  const e = expiryStatus(c.not_after);
                  return (
                    <li key={c.id} className="flex items-center gap-3 px-4 py-2.5">
                      <Link to="/t/$tenant/certificates" params={{ tenant }} className={LINK}>
                        {c.name}
                      </Link>
                      <span className="ml-auto">
                        <StatusPill status={e.status}>{e.label}</StatusPill>
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
          </Card>
          <Card title={t("Running jobs")} count={s.jobs.running + s.jobs.queued}>
            <p className="p-4 text-sm text-muted-foreground">
              {t("{running} running · {queued} queued · {failed} failed", {
                running: s.jobs.running,
                queued: s.jobs.queued,
                failed: s.jobs.failed,
              })}
            </p>
          </Card>
        </div>
        <div className="space-y-5">
          <Card title={t("Drifted engines")} count={s.drifted_engines.length}>
            {s.drifted_engines.length === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">
                {t("Every engine runs what the last revision rendered.")}
              </p>
            ) : (
              <ul className="divide-y text-[13px]">
                {s.drifted_engines.map((e) => (
                  <li key={e.engine_id} className="space-y-1 px-4 py-3">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{e.engine}</span>
                      <StatusPill status="degraded">{t("Drifted")}</StatusPill>
                      <span className="text-xs text-muted-foreground">{e.kinds.join(", ")}</span>
                    </div>
                    <p className="text-xs text-muted-foreground">
                      {t(
                        "Running config differs from the last revision. Reported only; nothing is changed automatically.",
                      )}
                    </p>
                    <ReapplyButton tenant={tenant} engine={e.engine} />
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card
            title={t("Open drafts")}
            action={
              <Link to="/t/$tenant/change-sets" params={{ tenant }} className={LINK}>
                {t("All change sets")}
              </Link>
            }
          >
            {(drafts.data?.items.length ?? 0) === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">{t("No open drafts.")}</p>
            ) : (
              <ul className="divide-y text-[13px]">
                {drafts.data?.items.map((c) => (
                  <li key={c.id} className="flex items-center gap-3 px-4 py-2.5">
                    <span className="size-2 rounded-full bg-draft" aria-hidden />
                    <Link to="/t/$tenant/review/$cs" params={{ tenant, cs: c.id }} className={LINK}>
                      {c.summary || t("(no summary)")}
                    </Link>
                    <span className="ml-auto text-muted-foreground">
                      {t("{n} changes", { n: c.op_count })}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
          <Card
            title={t("Recent revisions")}
            action={
              <Link to="/t/$tenant/revisions" params={{ tenant }} className={LINK}>
                {t("All revisions")}
              </Link>
            }
          >
            {(revisions.data?.items.length ?? 0) === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">{t("No revisions yet.")}</p>
            ) : (
              <ul className="divide-y text-[13px]">
                {revisions.data?.items.map((r) => (
                  <li key={r.number} className="flex items-center gap-3 px-4 py-2.5">
                    <span className="font-mono text-xs text-muted-foreground">#{r.number}</span>
                    <span className="min-w-0 flex-1 truncate">{r.summary}</span>
                    <span className="text-muted-foreground">{principalName(r.author)}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}
