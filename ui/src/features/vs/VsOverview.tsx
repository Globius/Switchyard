import { Link } from "@tanstack/react-router";
import { AlertTriangle, Zap } from "lucide-react";
import { useState } from "react";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { fieldOf, InvalidDraftCard } from "@/components/sy/Findings";
import { expiryStatus, StatusPill } from "@/components/sy/StatusPill";
import { ErrorState, TableSkeleton, UnavailableState } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import {
  usePools,
  useSetMemberState,
  useVs,
  useVsRendered,
  type AdminState,
  type PoolRead,
} from "./detail-queries";
import { useServiceFindings } from "./findings";
import { usePolicySets, useVsLogs } from "./overview-queries";
import { checkText, healthOf, rowsFor, worstOf, type MemberRow } from "./member-health";
import { useCertificates, useSeGroups, useVsMetrics, useVsStatus } from "./queries";

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
  enabled: <StatusPill status="neutral">{t("Enabled")}</StatusPill>,
  disabled: <StatusPill status="neutral">{t("Disabled")}</StatusPill>,
  drain: <StatusPill status="draining">{t("Draining")}</StatusPill>,
} as const;

const CARD = "rounded-lg border bg-card";
const CARD_HEAD = "border-b px-4 py-3";

/** A member's health pill with the reason of its last failed check. */
function MemberHealth({ row, engines }: { row: MemberRow | undefined; engines: number }) {
  if (!row) return <span className="text-muted-foreground">—</span>;
  const h = healthOf(row.status);
  const pill =
    h === "up" ? (
      <StatusPill status="healthy">{t("Up")}</StatusPill>
    ) : h === "down" ? (
      <StatusPill status="down">{t("Down")}</StatusPill>
    ) : h === "drain" ? (
      <StatusPill status="degraded">{t("Draining")}</StatusPill>
    ) : h === "maint" ? (
      <StatusPill status="neutral">{t("Maintenance")}</StatusPill>
    ) : (
      <StatusPill status="neutral">{t("Unknown")}</StatusPill>
    );
  const detail = checkText(row);
  return (
    <span className="flex flex-col gap-0.5">
      {pill}
      {(detail || engines > 1) && (
        <span className="text-xs text-muted-foreground">
          {detail}
          {engines > 1 ? `${detail ? " · " : ""}${t("{n} engines", { n: engines })}` : ""}
        </span>
      )}
    </span>
  );
}

/** Routes, members (runtime drain/enable/disable), listener, certificates, logs and the frontend. */
export function VsOverview({ tenant, id }: { tenant: string; id: string }) {
  const { draft } = useDraft(tenant);
  const [showLive, setShowLive] = useState(false);
  const overlay = showLive ? undefined : draft?.id;
  const vs = useVs(tenant, id, overlay);
  const pools = usePools(tenant, overlay);
  const livePools = usePools(tenant);
  const { findings, warnings } = useServiceFindings(tenant, vs.data?.name);
  const metrics = useVsMetrics(tenant);
  const certList = useCertificates(tenant, overlay);
  const groups = useSeGroups(tenant);
  const policySets = usePolicySets(tenant);
  const rendered = useVsRendered(tenant, id, overlay);
  const logs = useVsLogs(tenant, id);
  const status = useVsStatus(tenant);
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
  if (findings.length > 0 && !showLive)
    return (
      <InvalidDraftCard
        tenant={tenant}
        serviceId={id}
        findings={findings}
        onShowLive={() => setShowLive(true)}
      />
    );

  const v = vs.data;
  const byId = new Map(pools.data.items.map((p) => [p.id, p]));
  const used = [
    ...new Set([v.default_pool_id, ...v.routes.map((r) => r.pool_id)].filter(Boolean) as string[]),
  ];
  const usedPools = used.map((x) => byId.get(x)).filter((p) => p !== undefined);
  const result = setState.data;
  const svcStatus = status.data?.find((x) => x.id === id);
  const certs = (certList.data?.items ?? []).filter((c) =>
    (v.certificate_ids ?? []).includes(c.id),
  );
  const policyNames = new Map((policySets.data?.items ?? []).map((p) => [p.id, p.name]));
  const groupName = groups.data?.items.find((g) => g.id === v.se_group_id)?.name;
  const policyIds = v.policy_set_ids ?? [];
  const frontend = rendered.data?.items[0];
  const sorted = [...v.routes].sort((a, b) => a.order - b.order);
  const settings = "/t/$tenant/virtual-services/$id/settings" as const;

  return (
    <div className="space-y-6">
      {warnings.map((w, i) => (
        <section
          key={`${w.path}-${i}`}
          role="status"
          className="flex items-start gap-3 rounded-lg border border-status-degraded/40 bg-status-degraded-bg p-4 text-sm"
        >
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-status-degraded" aria-hidden />
          <div className="flex-1">
            <div className="font-semibold">{w.message}</div>
            <div className="font-mono text-xs text-muted-foreground">
              {w.path}
              {w.code ? ` · ${w.code}` : ""}
            </div>
          </div>
          <Button variant="outline" size="sm" asChild>
            <Link to={settings} params={{ tenant, id }} search={{ field: fieldOf(w.path) }}>
              {t("Fix in Settings")}
            </Link>
          </Button>
        </section>
      ))}

      <div className="grid gap-6 xl:grid-cols-[1fr_20rem]">
        <div className="min-w-0 space-y-6">
          <section className={CARD}>
            <header className="flex items-center gap-3 border-b px-4 py-3">
              <h2 className="text-base font-semibold">{t("Routes")}</h2>
              <span className="text-sm text-muted-foreground">{t("first match wins")}</span>
              <Button variant="outline" size="sm" className="ml-auto" asChild>
                <Link to={settings} params={{ tenant, id }} search={{ field: "routes" }}>
                  {t("Add route")}
                </Link>
              </Button>
            </header>
            {v.protocol !== "http" && (
              <p className="px-4 py-3 text-sm text-muted-foreground">
                {t("{p} services have no routes; all traffic goes to the default pool.", {
                  p: v.protocol.toUpperCase(),
                })}
              </p>
            )}
            <table className="w-full text-[13px]">
              <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="w-10 px-4 py-2 font-medium">#</th>
                  <th className="px-4 py-2 font-medium">{t("Host")}</th>
                  <th className="px-4 py-2 font-medium">{t("Path")}</th>
                  <th className="px-4 py-2 font-medium">{t("Target")}</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {sorted.map((r, i) => (
                  <tr key={`${r.order}-${i}`}>
                    <td className="px-4 py-3">{i + 1}</td>
                    <td className="px-4 py-3 font-mono">{r.match.host ?? t("any host")}</td>
                    <td className="px-4 py-3 font-mono">
                      {r.match.path_exact
                        ? `exact ${r.match.path_exact}`
                        : r.match.path_prefix
                          ? `prefix ${r.match.path_prefix}`
                          : "—"}
                    </td>
                    <td className="px-4 py-3">
                      {r.redirect ? (
                        t("Redirect {code} to {to}", {
                          code: r.redirect.code,
                          to: r.redirect.location,
                        })
                      ) : (
                        <>
                          {t("pool")}{" "}
                          <span className="text-primary">
                            {byId.get(r.pool_id ?? "")?.name ?? "—"}
                          </span>
                        </>
                      )}
                    </td>
                  </tr>
                ))}
                <tr className="text-muted-foreground">
                  <td className="px-4 py-3">—</td>
                  <td className="px-4 py-3">{t("default")}</td>
                  <td className="px-4 py-3">{t("everything else")}</td>
                  <td className="px-4 py-3">
                    {t("pool")}{" "}
                    <span className="text-primary">
                      {byId.get(v.default_pool_id ?? "")?.name ?? "—"}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
          </section>

          <section className={CARD}>
            <header className="flex flex-wrap items-center gap-3 border-b px-4 py-3">
              <h2 className="text-base font-semibold">{t("Members")}</h2>
              <Badge tone="live">
                <Zap className="size-3" aria-hidden /> {t("Live")}
              </Badge>
              <span className="text-sm text-muted-foreground">
                {t("Drain, enable and disable act immediately, outside the change set")}
              </span>
            </header>
            {result && Object.keys(result.failed).length > 0 && (
              <div
                role="alert"
                className="m-4 rounded-lg border border-status-degraded/40 bg-status-degraded-bg p-3 text-sm"
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
            {usedPools.length === 0 ? (
              <p className="px-4 py-3 text-sm text-muted-foreground">
                {t("This service sends traffic to no pool yet.")}
              </p>
            ) : (
              <table className="w-full text-[13px]">
                <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
                  <tr>
                    {[
                      "Pool",
                      "Member",
                      "Address",
                      "Health",
                      "Weight",
                      "Admin state",
                      "Runtime",
                    ].map((h) => (
                      <th key={h} className="px-4 py-2 font-medium">
                        {t(h)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {usedPools.flatMap((p) =>
                    p.members.map((m) => {
                      const fresh = notApplied(p, m.id, livePools.data?.items);
                      return (
                        <tr
                          key={`${p.id}-${m.id}`}
                          className={fresh ? "bg-draft-bg/40" : undefined}
                        >
                          <td className="px-4 py-3">{p.name}</td>
                          <td className="px-4 py-3 font-mono">
                            {fresh && (
                              <span
                                className="mr-1.5 inline-block size-2 rounded-full bg-draft"
                                aria-hidden
                              />
                            )}
                            {m.name ?? m.address}
                          </td>
                          <td className="px-4 py-3 font-mono">
                            {m.address}
                            {m.port ? `:${m.port}` : ""}
                          </td>
                          <td className="px-4 py-3">
                            {fresh ? (
                              <span className="text-muted-foreground">—</span>
                            ) : (
                              <MemberHealth
                                row={worstOf(rowsFor(svcStatus?.members, p.id, m.id))}
                                engines={rowsFor(svcStatus?.members, p.id, m.id).length}
                              />
                            )}
                          </td>
                          <td className="px-4 py-3">{m.weight}</td>
                          <td className="px-4 py-3">
                            {fresh ? (
                              <StatusPill status="draft">{t("Not applied")}</StatusPill>
                            ) : (
                              STATE_PILL[m.admin_state]
                            )}
                          </td>
                          <td className="px-4 py-3">
                            {fresh ? (
                              <span className="text-muted-foreground">{t("after apply")}</span>
                            ) : (
                              <span className="flex gap-1.5">
                                {(["enabled", "drain", "disabled"] as const)
                                  .filter((st) => st !== m.admin_state)
                                  .map((st) => (
                                    <Tip key={st} label={perm.reason}>
                                      <Button
                                        variant="outline"
                                        size="sm"
                                        disabled={!perm.allowed}
                                        onClick={() =>
                                          setPending({ pool: p, member: m, state: st })
                                        }
                                      >
                                        {st === "enabled"
                                          ? t("Enable")
                                          : st === "drain"
                                            ? t("Drain")
                                            : t("Disable")}
                                      </Button>
                                    </Tip>
                                  ))}
                              </span>
                            )}
                          </td>
                        </tr>
                      );
                    }),
                  )}
                </tbody>
              </table>
            )}
            <p className="border-t px-4 py-3 text-xs text-muted-foreground">
              {t(
                "Health comes from engine status: the worst state any engine reports, with the reason of the last failed check. Admin state is what the config says; the Live buttons act at runtime.",
              )}
            </p>
          </section>

          <section className={CARD}>
            <header className={CARD_HEAD}>
              <h2 className="text-base font-semibold">{t("Latency breakdown")}</h2>
            </header>
            <div className="p-4">
              {metrics.data && !metrics.data.available ? (
                <UnavailableState title={t("Metrics unavailable")} reason={metrics.data.reason} />
              ) : (
                <p className="text-sm text-muted-foreground">
                  {t("Open the Grafana dashboard for latency by backend.")}
                </p>
              )}
            </div>
          </section>
        </div>

        <aside className="space-y-6">
          <section className={CARD}>
            <header className={CARD_HEAD}>
              <h2 className="text-base font-semibold">{t("Listener")}</h2>
            </header>
            <dl className="grid grid-cols-[7rem_1fr] gap-y-2 p-4 text-[13px]">
              <dt className="text-muted-foreground">VIP</dt>
              <dd className="font-mono">
                {v.vip ?? t("allocated on apply")}
                {v.vip_pool_id ? "" : ` ${t("(fixed)")}`}
              </dd>
              {v.ports.map((p) => (
                <div key={p.port} className="contents">
                  <dt className="text-muted-foreground">{t("Port {n}", { n: p.port })}</dt>
                  <dd>
                    {[p.tls ? "TLS" : "", p.http2 ? "HTTP/2" : "", p.proxy_protocol ? "PROXY" : ""]
                      .filter(Boolean)
                      .join(" · ") || t("plain")}
                  </dd>
                </div>
              ))}
              {v.restore_client_ip_header && (
                <>
                  <dt className="text-muted-foreground">{t("Client IP header")}</dt>
                  <dd>{v.restore_client_ip_header}</dd>
                </>
              )}
              {(policyIds.length > 0 || v.allow_list_id) && (
                <>
                  <dt className="text-muted-foreground">{t("Policies")}</dt>
                  <dd>
                    {[...policyIds, ...(v.allow_list_id ? [v.allow_list_id] : [])]
                      .map((x) => policyNames.get(x) ?? x)
                      .join(", ")}
                    {v.allow_list_id && v.enforce_allow_list ? ` · ${t("enforced")}` : ""}
                  </dd>
                </>
              )}
              <dt className="text-muted-foreground">{t("SE group")}</dt>
              <dd>{groupName ?? "—"}</dd>
            </dl>
          </section>

          <section className={CARD}>
            <header className={CARD_HEAD}>
              <h2 className="text-base font-semibold">{t("Certificates")}</h2>
            </header>
            <div className="space-y-3 p-4 text-[13px]">
              {certs.length === 0 && (
                <p className="text-muted-foreground">
                  {t("No certificate; this service does not terminate TLS.")}
                </p>
              )}
              {certs.map((c) => {
                const ex = expiryStatus(c.not_after);
                return (
                  <div key={c.id} className="space-y-1">
                    <div className="font-medium text-primary">{c.name}</div>
                    <StatusPill status={ex.status}>
                      {ex.status === "neutral"
                        ? ex.label
                        : t("Expires in {n} days", { n: ex.days })}
                    </StatusPill>
                    <div className="font-mono text-xs text-muted-foreground">
                      {c.sans.join(", ")} · {c.source} · {t("renew {r}", { r: c.renew_policy })}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>

          <section className={CARD}>
            <header className={CARD_HEAD}>
              <h2 className="text-base font-semibold">{t("Access log")}</h2>
            </header>
            <div className="p-4">
              {logs.data && !logs.data.available ? (
                <UnavailableState title={t("Logs unavailable")} reason={logs.data.reason} />
              ) : logs.data && logs.data.entries.length > 0 ? (
                <ul className="space-y-1 font-mono text-xs">
                  {logs.data.entries.slice(0, 8).map((e, i) => (
                    <li key={i}>
                      {e.status} {e.method} {e.path} · {e.duration_ms} ms
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">{t("No recent requests.")}</p>
              )}
            </div>
          </section>

          {frontend && (
            <section className={CARD}>
              <header className="flex items-center border-b px-4 py-3">
                <h2 className="text-base font-semibold">
                  {t("Rendered frontend")}
                  {draft && !showLive ? ` · ${t("with draft")}` : ""}
                </h2>
                <Link
                  to="/t/$tenant/virtual-services/$id/rendered"
                  params={{ tenant, id }}
                  className="ml-auto text-sm text-primary hover:underline"
                >
                  {t("Full file")}
                </Link>
              </header>
              <pre className="max-h-56 overflow-auto p-4 font-mono text-xs leading-5">
                {frontend.content
                  .split("\n")
                  .slice((frontend.range?.[0] ?? 1) - 1, frontend.range?.[1] ?? 12)
                  .join("\n")}
              </pre>
            </section>
          )}
        </aside>
      </div>

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
