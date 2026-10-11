import { useState } from "react";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { ErrorState, TableSkeleton, UnavailableState } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { useVsMetrics } from "./queries";
import {
  usePools,
  useSetMemberState,
  useVs,
  type AdminState,
  type PoolRead,
} from "./detail-queries";

type Member = PoolRead["members"][number];

/** Human text for a route match: host, then path. */
export function describeMatch(m: {
  host?: string | null;
  path_prefix?: string | null;
  path_exact?: string | null;
}) {
  const parts = [
    m.host,
    m.path_exact ? `= ${m.path_exact}` : m.path_prefix ? `${m.path_prefix}*` : null,
  ];
  return parts.filter(Boolean).join(" ") || t("any request");
}

/** A member that exists only in the draft: its pool is new, or the live pool does not list it. */
export function notApplied(
  pool: { id: string; draft_status: string },
  memberId: string,
  live: { id: string; members: { id: string }[] }[] | undefined,
): boolean {
  if (pool.draft_status === "created") return true;
  if (pool.draft_status !== "modified" || !live) return false;
  const lp = live.find((x) => x.id === pool.id);
  return lp ? !lp.members.some((m) => m.id === memberId) : false;
}

const STATE_PILL = {
  enabled: <StatusPill status="healthy">{t("Enabled")}</StatusPill>,
  disabled: <StatusPill status="neutral">{t("Disabled")}</StatusPill>,
  drain: <StatusPill status="draining">{t("Draining")}</StatusPill>,
} as const;

/** Routes, pools with members (runtime drain/enable/disable), and the metrics note. */
export function VsOverview({ tenant, id }: { tenant: string; id: string }) {
  const { draft } = useDraft(tenant);
  const vs = useVs(tenant, id, draft?.id);
  const pools = usePools(tenant, draft?.id);
  const livePools = usePools(tenant);
  const metrics = useVsMetrics(tenant);
  const can = useCan(tenant);
  const setState = useSetMemberState(tenant);
  const [pending, setPending] = useState<{
    pool: PoolRead;
    member: Member;
    state: AdminState;
  } | null>(null);
  const perm = can("set_state", "member");

  if (vs.isPending || pools.isPending) return <TableSkeleton rows={4} cols={3} />;
  if (vs.isError) return <ErrorState error={vs.error} onRetry={() => void vs.refetch()} />;
  if (pools.isError) return <ErrorState error={pools.error} onRetry={() => void pools.refetch()} />;
  const v = vs.data;
  const byId = new Map(pools.data.items.map((p) => [p.id, p]));
  const used = new Set(
    [v.default_pool_id, ...v.routes.map((r) => r.pool_id)].filter(Boolean) as string[],
  );
  const result = setState.data;

  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">{t("Routes")}</h2>
        {v.protocol !== "http" ? (
          <p className="text-sm text-muted-foreground">
            {t("{p} services have no routes; all traffic goes to the default pool.", {
              p: v.protocol.toUpperCase(),
            })}
          </p>
        ) : null}
        <div className="overflow-x-auto rounded-lg border bg-card">
          <table className="w-full text-[13px]">
            <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                <th className="px-4 py-2.5 font-medium">#</th>
                <th className="px-4 py-2.5 font-medium">{t("Match")}</th>
                <th className="px-4 py-2.5 font-medium">{t("Goes to")}</th>
              </tr>
            </thead>
            <tbody className="divide-y">
              {[...v.routes]
                .sort((a, b) => a.order - b.order)
                .map((r, i) => (
                  <tr key={`${r.order}-${i}`}>
                    <td className="px-4 py-3">{i + 1}</td>
                    <td className="px-4 py-3 font-mono">{describeMatch(r.match)}</td>
                    <td className="px-4 py-3">
                      {r.redirect
                        ? t("Redirect {code} to {to}", {
                            code: r.redirect.code,
                            to: r.redirect.location,
                          })
                        : (byId.get(r.pool_id ?? "")?.name ?? "—")}
                    </td>
                  </tr>
                ))}
              <tr>
                <td className="px-4 py-3 text-muted-foreground">{t("default")}</td>
                <td className="px-4 py-3 text-muted-foreground">{t("everything else")}</td>
                <td className="px-4 py-3">{byId.get(v.default_pool_id ?? "")?.name ?? "—"}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section className="space-y-3">
        <div className="flex items-center gap-2">
          <h2 className="text-lg font-semibold">{t("Pools and members")}</h2>
          <Badge tone="live">{t("Live actions")}</Badge>
        </div>
        {result && Object.keys(result.failed).length > 0 && (
          <div
            role="alert"
            className="rounded-lg border border-status-degraded/40 bg-status-degraded-bg p-3 text-sm"
          >
            <div className="font-semibold">
              {t("Applied on {a} engine(s), failed on {f}", {
                a: result.applied.length,
                f: Object.keys(result.failed).length,
              })}
            </div>
            {Object.entries(result.failed).map(([engine, why]) => (
              <div key={engine} className="font-mono text-xs">
                {engine}: {why}
              </div>
            ))}
          </div>
        )}
        {[...used]
          .map((pid) => byId.get(pid))
          .filter((p) => p !== undefined)
          .map((p) => (
            <div key={p.id} className="rounded-lg border bg-card">
              <div className="flex items-center gap-3 border-b px-4 py-2.5">
                <span className="font-medium">{p.name}</span>
                <span className="text-xs text-muted-foreground">
                  {p.algorithm} · {t("{n} members", { n: p.members.length })}
                </span>
              </div>
              <ul className="divide-y">
                {p.members.map((m) => (
                  <li key={m.id} className="flex items-center gap-3 px-4 py-2.5 text-[13px]">
                    <span className="font-mono">{m.name ?? m.address}</span>
                    <span className="font-mono text-muted-foreground">
                      {m.address}
                      {m.port ? `:${m.port}` : ""}
                    </span>
                    <span className="ml-auto flex items-center gap-2">
                      {notApplied(p, m.id, livePools.data?.items) && (
                        <StatusPill status="draft">{t("Not applied")}</StatusPill>
                      )}
                      {STATE_PILL[m.admin_state]}
                    </span>
                    {(["enabled", "drain", "disabled"] as const)
                      .filter((s) => s !== m.admin_state)
                      .map((s) => (
                        <Tip key={s} label={perm.reason}>
                          <Button
                            variant="outline"
                            size="sm"
                            disabled={!perm.allowed}
                            onClick={() => setPending({ pool: p, member: m, state: s })}
                          >
                            {s === "enabled"
                              ? t("Enable")
                              : s === "drain"
                                ? t("Drain")
                                : t("Disable")}
                          </Button>
                        </Tip>
                      ))}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        {used.size === 0 && (
          <p className="text-sm text-muted-foreground">
            {t("This service sends traffic to no pool yet.")}
          </p>
        )}
      </section>

      {metrics.data && !metrics.data.available && (
        <UnavailableState title={t("Metrics unavailable")} reason={metrics.data.reason} />
      )}

      <ConfirmDialog
        open={pending !== null}
        onOpenChange={(o) => !o && setPending(null)}
        title={t("Change member state?")}
        confirmLabel={t("Apply now")}
        busy={setState.isPending}
        onConfirm={() => {
          if (!pending) return;
          setState.mutate(
            { pool: pending.pool.id, member: pending.member.id, state: pending.state },
            { onSettled: () => setPending(null) },
          );
        }}
      >
        <p>
          {t("Set {m} in {p} to {s} on every engine now.", {
            m: pending?.member.name ?? pending?.member.address ?? "",
            p: pending?.pool.name ?? "",
            s: pending?.state ?? "",
          })}
        </p>
        <p>
          {t("This is a live action: it skips the change set and is recorded in the audit log.")}
        </p>
      </ConfirmDialog>
    </div>
  );
}
