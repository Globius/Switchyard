import { Link } from "@tanstack/react-router";
import { ArrowLeft } from "lucide-react";
import { useState } from "react";
import { ApiError } from "@/api/client";
import { useCan, useMe } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { ConfigEditor } from "@/components/sy/ConfigEditor";
import { StatusPill } from "@/components/sy/StatusPill";
import { ErrorState, TableSkeleton } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { StatePill } from "@/features/changesets/ChangeSets";
import { t } from "@/lib/i18n";
import { show } from "@/lib/snapshot-diff";
import { cn } from "@/lib/utils";
import { ConflictResolver } from "./ConflictResolver";
import { JobProgress } from "./JobProgress";
import { isListField, ListDiff } from "./ListDiff";
import {
  blocked,
  countsLine,
  describeCreated,
  describeDeleted,
  parseUnified,
  phaseOf,
  TYPE_LABEL,
  TYPE_PATH,
  type Diff,
} from "./model";
import {
  useApplyJob,
  useChangeSetActions,
  useChangeSetRead,
  useDiff,
  useLabels,
  useObjectChange,
  useTenantSettings,
} from "./queries";

type Tab = "objects" | "config" | "impact";

/** Where "Open" takes you for an object of this type. */
function OpenLink({ tenant, type, id }: { tenant: string; type: string; id: string }) {
  if (type === "virtual_service")
    return (
      <Link
        to="/t/$tenant/virtual-services/$id"
        params={{ tenant, id }}
        className="ml-auto text-sm text-primary hover:underline"
      >
        {t("Open")}
      </Link>
    );
  const section = TYPE_PATH[type];
  return section ? (
    <Link
      to="/t/$tenant/$"
      params={{ tenant, _splat: section }}
      className="ml-auto text-sm text-primary hover:underline"
    >
      {t("Open")}
    </Link>
  ) : null;
}

function ObjectCard({
  tenant,
  cs,
  baseRevision,
  o,
  labels,
}: {
  tenant: string;
  cs: string;
  baseRevision: number;
  o: Diff["objects"][number];
  labels: Record<string, string>;
}) {
  const q = useObjectChange(tenant, o.type, o.id, cs, o.status);
  const deleted = o.status === "deleted";
  const created = o.status === "created";
  const status = o.status === "unchanged" ? "modified" : o.status;
  return (
    <section className="rounded-lg border bg-card">
      <header className="flex flex-wrap items-center gap-2 border-b px-4 py-2.5 text-sm">
        <Badge>{t(TYPE_LABEL[o.type] ?? o.type)}</Badge>
        <span className={cn("font-semibold", deleted && "line-through")}>{o.name}</span>
        <StatusPill status="draft">
          {status === "modified"
            ? t("Modified · {n} fields", { n: o.changed_fields.length })
            : status === "created"
              ? t("Created")
              : t("Deleted")}
        </StatusPill>
        {created && q.data?.draft && (
          <span className="text-muted-foreground">
            {describeCreated(o.type, q.data.draft, labels)}
          </span>
        )}
        {deleted && (
          <span className="text-muted-foreground">
            {describeDeleted(o.type, q.data?.live, labels)}
          </span>
        )}
        {!deleted && <OpenLink tenant={tenant} type={o.type} id={o.id} />}
      </header>
      {q.isPending && !deleted && !created ? (
        <TableSkeleton rows={2} cols={2} />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : deleted || created ? null : (
        <table className="w-full text-[13px]">
          <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              <th className="w-56 px-4 py-2 font-medium">{t("Field")}</th>
              <th className="px-4 py-2 font-medium">{t("Live (rev #{n})", { n: baseRevision })}</th>
              <th className="px-4 py-2 font-medium">{t("Draft")}</th>
            </tr>
          </thead>
          <tbody className="divide-y font-mono text-xs">
            {q.data!.fields.map((f) =>
              isListField(f.field, f.before, f.after) ? (
                <tr key={f.field}>
                  <td className="px-4 py-3 align-top">{f.field}</td>
                  <td colSpan={2} className="px-4 py-3">
                    <ListDiff
                      field={f.field}
                      before={f.before}
                      after={f.after}
                      labels={labels}
                      revision={baseRevision}
                    />
                  </td>
                </tr>
              ) : (
                <tr key={f.field}>
                  <td className="px-4 py-2">{f.field}</td>
                  <td className="px-4 py-2">
                    <span className="bg-diff-del px-1">− {show(f.before)}</span>
                  </td>
                  <td className="px-4 py-2">
                    <span className="bg-diff-add px-1">+ {show(f.after)}</span>
                  </td>
                </tr>
              ),
            )}
          </tbody>
        </table>
      )}
    </section>
  );
}

function ConfigTab({ diff }: { diff: Diff }) {
  return (
    <div className="space-y-4">
      {diff.se_groups.map((g) => (
        <section key={g.id} className="rounded-lg border bg-card">
          <header className="flex items-center gap-2 border-b px-4 py-2.5 text-sm">
            <span className="font-semibold">{g.name}</span>
            {g.error ? (
              <StatusPill status="down">{t("Render failed")}</StatusPill>
            ) : g.files.length === 0 ? (
              <span className="text-muted-foreground">{t("no changes")}</span>
            ) : (
              <StatusPill status="draft">{t("changed")}</StatusPill>
            )}
          </header>
          {g.error && (
            <p role="alert" className="px-4 py-3 text-sm text-status-down">
              {g.error}
            </p>
          )}
          {g.files.map((f) => (
            <div key={f.path} className="border-b last:border-b-0">
              <div className="flex items-center gap-3 bg-muted/40 px-4 py-1.5 font-mono text-xs">
                <span>{f.path}</span>
                <span className="text-muted-foreground">{f.status}</span>
              </div>
              <ConfigEditor
                content={f.diff}
                language="sy-diff"
                label={f.path}
                fallback={
                  <pre className="overflow-x-auto py-1 font-mono text-[12px] leading-5">
                    {parseUnified(f.diff).map((l, i) => (
                      <div
                        key={i}
                        className={cn(
                          "flex px-4",
                          l.kind === "add" && "bg-diff-add",
                          l.kind === "del" && "bg-diff-del",
                          l.kind === "hunk" && "bg-muted/60 text-muted-foreground",
                        )}
                      >
                        <span aria-hidden className="w-4 shrink-0">
                          {l.kind === "add" ? "+" : l.kind === "del" ? "−" : " "}
                        </span>
                        <span className="sr-only">
                          {l.kind === "add" ? t("added") : l.kind === "del" ? t("removed") : ""}
                        </span>
                        <code>{l.text || " "}</code>
                      </div>
                    ))}
                  </pre>
                }
              />
            </div>
          ))}
        </section>
      ))}
      {diff.se_groups.length === 0 && (
        <p className="text-sm text-muted-foreground">{t("No SE group's configuration changes.")}</p>
      )}
    </div>
  );
}

function ImpactTab({ diff }: { diff: Diff }) {
  const list = (items: string[]) => (items.length ? items.join(", ") : "—");
  return (
    <div className="space-y-5">
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">{t("Validator")}</h2>
        {diff.problems.length === 0 && diff.warnings.length === 0 && (
          <p className="text-sm text-muted-foreground">
            {t("No problems found. Checked with the draft diff, without saving.")}
          </p>
        )}
        {diff.problems.map((p, i) => (
          <div
            key={`p${i}`}
            role="alert"
            className="rounded-lg border border-status-down/30 bg-status-down-bg p-3 text-sm"
          >
            <div className="font-semibold text-status-down">
              {t("Blocking")}: {p.message}
            </div>
            <div className="font-mono text-xs text-muted-foreground">
              {p.path} · {p.code}
            </div>
          </div>
        ))}
        {diff.warnings.map((w, i) => (
          <div
            key={`w${i}`}
            className="rounded-lg border border-status-degraded/30 bg-status-degraded-bg p-3 text-sm"
          >
            <div className="font-semibold text-status-degraded">
              {t("Warning")}: {w.message}
            </div>
            <div className="font-mono text-xs text-muted-foreground">
              {w.path} · {w.code}
            </div>
          </div>
        ))}
      </section>
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">{t("What this touches")}</h2>
        <dl className="grid grid-cols-[8rem_1fr] gap-y-2 rounded-lg border bg-card p-4 text-sm">
          <dt className="text-muted-foreground">{t("VIPs")}</dt>
          <dd className="font-mono">{list(diff.impact.vips)}</dd>
          <dt className="text-muted-foreground">{t("Services")}</dt>
          <dd>{list(diff.impact.services.map((s) => s.name))}</dd>
          <dt className="text-muted-foreground">{t("SE groups")}</dt>
          <dd>{list(diff.impact.se_groups.map((g) => g.name))}</dd>
          <dt className="text-muted-foreground">{t("Hostnames")}</dt>
          <dd className="font-mono">{list(diff.impact.hostnames)}</dd>
        </dl>
      </section>
    </div>
  );
}

/** Full-screen review: Objects, Config and Impact tabs beside the apply, approval and progress panel. */
export function Review({ tenant, cs }: { tenant: string; cs: string }) {
  const [tab, setTab] = useState<Tab>("objects");
  const [ack, setAck] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [comment, setComment] = useState("");
  const [bypass, setBypass] = useState(false);
  const [reason, setReason] = useState("");
  const [jobId, setJobId] = useState<string | null>(null);
  const [problem, setProblem] = useState<unknown>(null);

  const { data: me } = useMe();
  const can = useCan(tenant);
  const set = useChangeSetRead(tenant, cs);
  const settings = useTenantSettings(tenant);
  const diffEnabled = set.data
    ? set.data.state === "draft" ||
      set.data.state === "pending_approval" ||
      set.data.state === "failed"
    : false;
  const diff = useDiff(tenant, cs, diffEnabled);
  const job = useApplyJob(tenant, cs, jobId, true);
  const actions = useChangeSetActions(tenant, cs);
  const labels = useLabels(tenant, cs);

  if (set.isPending)
    return (
      <div className="p-8">
        <TableSkeleton rows={4} cols={3} />
      </div>
    );
  if (set.isError)
    return (
      <div className="p-8">
        <ErrorState error={set.error} onRetry={() => void set.refetch()} />
      </div>
    );
  const c = set.data;
  const phase = phaseOf(c.state, job.data ?? undefined);
  const requireApproval = settings.data?.require_approval ?? false;
  const mine = me?.principal !== undefined && c.author === me.principal;
  const warnings = diff.data?.warnings.length ?? 0;
  const invalid = phase === "ready" && (diff.data?.problems.length ?? 0) > 0;
  const noGo = blocked(diff.data) || (warnings > 0 && !ack);
  const canApply = can("apply", "change_set");
  const canRequest = can("request_approval", "change_set");
  const canApprove = can("approve", "change_set");
  const canReject = can("reject", "change_set");
  const canBypass = can("apply_bypass", "break_glass");

  async function run<T>(f: () => Promise<T>): Promise<T | undefined> {
    setProblem(null);
    try {
      return await f();
    } catch (e) {
      setProblem(e);
      return undefined;
    }
  }
  const startApply = async (r: string | null) => {
    const res = await run(() => actions.apply.mutateAsync(r));
    if (res?.job_id) setJobId(res.job_id);
  };
  const conflict =
    problem instanceof ApiError && problem.status === 412 ? problem.problem : undefined;
  const stale = conflict?.conflict ?? undefined;

  return (
    <div className="min-h-screen bg-background">
      <header className="flex flex-wrap items-center gap-4 border-b bg-card px-6 py-3">
        <Button variant="ghost" asChild>
          <Link to="/t/$tenant/change-sets" params={{ tenant }}>
            <ArrowLeft /> {t("Back to editing")}
          </Link>
        </Button>
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-lg font-semibold">{t("Review change set")}</h1>
            <StatePill state={c.state} />
          </div>
          <p className="text-xs text-muted-foreground">
            {c.summary} · {tenant} · {t("base revision #{n}", { n: c.base_revision })}
            {diff.data
              ? ` · ${t("{n} changes", { n: diff.data.objects.length })}: ${countsLine(diff.data.objects)}`
              : ""}
          </p>
        </div>
        <Badge className="ml-auto">
          {t("Tenant")} {tenant}
        </Badge>
      </header>

      <div className="mx-auto grid max-w-7xl gap-6 p-6 lg:grid-cols-[1fr_22rem]">
        <main className="min-w-0 space-y-4">
          {!diffEnabled ? (
            <p className="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
              {t("This change set is {state}; there is nothing left to review.", {
                state: c.state,
              })}
            </p>
          ) : (
            <>
              {stale && (
                <ConflictResolver
                  tenant={tenant}
                  cs={cs}
                  conflict={stale}
                  changeSet={c}
                  onResolved={() => setProblem(null)}
                />
              )}
              {invalid && diff.data && (
                <section
                  role="alert"
                  aria-label={t("Your draft is invalid")}
                  className="space-y-2 rounded-lg border border-status-down/30 bg-status-down-bg p-4 text-sm"
                >
                  <div className="font-semibold text-status-down">{t("Your draft is invalid")}</div>
                  <p>{t("It cannot be applied until these findings are fixed.")}</p>
                  <ul className="space-y-1">
                    {diff.data.problems.map((p, i) => (
                      <li key={`${p.path}-${i}`}>
                        {p.message}{" "}
                        <span className="font-mono text-xs text-muted-foreground">{p.path}</span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
              <div role="tablist" className="flex gap-1 border-b">
                {(["objects", "config", "impact"] as const).map((k) => (
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
                    {{ objects: t("Objects"), config: t("Config"), impact: t("Impact") }[k]}
                    {k === "objects" && diff.data && (
                      <span className="ml-2 rounded-full bg-secondary px-2 py-0.5 text-xs">
                        {diff.data.objects.length}
                      </span>
                    )}
                    {k === "config" &&
                      diff.data &&
                      diff.data.se_groups.some((g) => g.files.length > 0 || g.error) && (
                        <span className="ml-2 rounded-full bg-secondary px-2 py-0.5 text-xs">
                          {t("{n} SE group", {
                            n: diff.data.se_groups.filter((g) => g.files.length > 0 || g.error)
                              .length,
                          })}
                        </span>
                      )}
                    {k === "impact" && diff.data && diff.data.problems.length > 0 && (
                      <span className="ml-2 rounded-full bg-status-down-bg px-2 py-0.5 text-xs text-status-down">
                        {t("{n} problems", { n: diff.data.problems.length })}
                      </span>
                    )}
                    {k === "impact" &&
                      diff.data &&
                      diff.data.problems.length === 0 &&
                      diff.data.warnings.length > 0 && (
                        <span className="ml-2 rounded-full bg-status-degraded-bg px-2 py-0.5 text-xs text-status-degraded">
                          {t(diff.data.warnings.length === 1 ? "{n} warning" : "{n} warnings", {
                            n: diff.data.warnings.length,
                          })}
                        </span>
                      )}
                  </button>
                ))}
              </div>
              {diff.isPending ? (
                <TableSkeleton rows={4} cols={3} />
              ) : diff.isError ? (
                <ErrorState error={diff.error} onRetry={() => void diff.refetch()} />
              ) : tab === "objects" ? (
                <div className="space-y-3">
                  {diff.data.objects.map((o) => (
                    <ObjectCard
                      key={o.id}
                      tenant={tenant}
                      cs={cs}
                      baseRevision={c.base_revision}
                      o={o}
                      labels={labels}
                    />
                  ))}
                  <p className="text-xs text-muted-foreground">
                    {t(
                      "Before and after values are computed in the browser from live and draft reads.",
                    )}
                  </p>
                </div>
              ) : tab === "config" ? (
                invalid ? (
                  <p className="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
                    {t("The configuration can't be rendered while the draft is invalid.")}
                  </p>
                ) : (
                  <ConfigTab diff={diff.data} />
                )
              ) : (
                <ImpactTab diff={diff.data} />
              )}
            </>
          )}
        </main>

        <aside className="h-fit space-y-4 rounded-lg border bg-card p-5" aria-label={t("Apply")}>
          <h2 className="font-semibold">{t("Apply")}</h2>

          {phase === "ready" && (
            <>
              <div className="space-y-1">
                <div className="text-sm font-medium">{t("Revision message")}</div>
                <p className="rounded-md border bg-muted/40 p-2 text-sm">{c.summary}</p>
              </div>
              {warnings > 0 && (
                <label className="flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
                  {t("I have reviewed {n} validator warnings", { n: warnings })}
                </label>
              )}
              {diff.data && diff.data.problems.length > 0 && (
                <p role="alert" className="text-sm text-status-down">
                  {t("{n} blocking validator problems must be fixed first.", {
                    n: diff.data.problems.length,
                  })}
                </p>
              )}
              {requireApproval ? (
                <>
                  <p className="text-xs text-muted-foreground">
                    {t(
                      "{tenant} requires approval. A tenant-admin other than you approves it, and approving applies it, canary first. A failed canary reverts and leaves this draft open.",
                      { tenant },
                    )}
                  </p>
                  <Tip label={canRequest.reason}>
                    <Button
                      className="w-full"
                      disabled={!canRequest.allowed || noGo || actions.requestApproval.isPending}
                      onClick={() => void run(() => actions.requestApproval.mutateAsync())}
                    >
                      {t("Request approval")}
                    </Button>
                  </Tip>
                  {canBypass.allowed && (
                    <Button
                      variant="outline"
                      className="w-full"
                      disabled={noGo}
                      onClick={() => setBypass(true)}
                    >
                      {t("Apply anyway (break-glass)")}
                    </Button>
                  )}
                </>
              ) : (
                <Tip label={canApply.reason}>
                  <Button
                    className="w-full"
                    disabled={!canApply.allowed || noGo || actions.apply.isPending}
                    onClick={() => void startApply(null)}
                  >
                    {t("Apply")}
                  </Button>
                </Tip>
              )}
            </>
          )}

          {phase === "pending_approval" && (
            <>
              <StatusPill status="pending">{t("Pending approval")}</StatusPill>
              <p className="text-sm">
                {t("{tenant} requires a second person to approve.", { tenant })}
              </p>
              {mine ? (
                <p className="text-sm text-muted-foreground">
                  {t(
                    "You wrote this change set, so you can't approve it. Another tenant-admin has to.",
                  )}
                </p>
              ) : (
                <>
                  {rejecting && (
                    <label className="block space-y-1 text-sm">
                      {t("Comment (required to reject)")}
                      <textarea
                        aria-label={t("Rejection comment")}
                        className="w-full rounded-md border bg-card p-2 text-sm"
                        rows={3}
                        value={comment}
                        onChange={(e) => setComment(e.target.value)}
                      />
                    </label>
                  )}
                  <div className="flex gap-2">
                    <Tip label={canReject.reason}>
                      <Button
                        variant="outline"
                        disabled={!canReject.allowed || (rejecting && !comment.trim())}
                        onClick={() =>
                          rejecting
                            ? void run(() => actions.reject.mutateAsync(comment.trim()))
                            : setRejecting(true)
                        }
                      >
                        {t("Reject")}
                      </Button>
                    </Tip>
                    <Tip label={canApprove.reason ?? undefined}>
                      <Button
                        disabled={!canApprove.allowed || actions.approve.isPending}
                        onClick={() =>
                          void run(async () => {
                            const r = await actions.approve.mutateAsync();
                            if (r?.job_id) setJobId(r.job_id);
                          })
                        }
                      >
                        {t("Approve and apply")}
                      </Button>
                    </Tip>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    {t("Approving applies it at once, with the canary step.")}
                  </p>
                </>
              )}
            </>
          )}

          {(phase === "applying" ||
            phase === "retrying" ||
            phase === "reverted" ||
            phase === "gave_up" ||
            phase === "failed") &&
            job.data && (
              <>
                <div>
                  {phase === "applying" && (
                    <StatusPill status="pending">{t("Applying")}</StatusPill>
                  )}
                  {phase === "retrying" && (
                    <StatusPill status="degraded">{t("Retrying after error")}</StatusPill>
                  )}
                  {phase === "reverted" && <StatusPill status="down">{t("Reverted")}</StatusPill>}
                  {phase === "gave_up" && (
                    <StatusPill status="down">{t("Dead · gave up")}</StatusPill>
                  )}
                  {phase === "failed" && <StatusPill status="down">{t("Failed")}</StatusPill>}
                </div>
                <JobProgress stages={job.data.stages} />
                {(job.data.error ||
                  (job.data.result as { error?: string | null } | null)?.error) && (
                  <p role="alert" className="text-sm text-status-down">
                    {job.data.error ?? (job.data.result as { error?: string }).error}
                  </p>
                )}
                {phase === "reverted" && (
                  <p className="text-xs text-muted-foreground">
                    {t(
                      "Not an infrastructure error, so it will not retry on its own. This draft is still open.",
                    )}
                  </p>
                )}
                {(phase === "reverted" || phase === "failed" || phase === "gave_up") && (
                  <Tip label={canApply.reason}>
                    <Button
                      className="w-full"
                      disabled={!canApply.allowed}
                      onClick={() => void startApply(null)}
                    >
                      {t("Apply again")}
                    </Button>
                  </Tip>
                )}
              </>
            )}

          {phase === "applied" && <StatusPill status="healthy">{t("Applied")}</StatusPill>}
          {phase === "discarded" && <StatusPill status="neutral">{t("Discarded")}</StatusPill>}

          {conflict && (
            <div
              role="alert"
              className="rounded-md border border-status-degraded/40 bg-status-degraded-bg p-3 text-sm"
            >
              <div className="font-semibold">
                {t("Someone else changed an object in this draft")}
              </div>
              <p>{t("Pick a value for each conflicting field, then apply again.")}</p>
            </div>
          )}
          {problem != null && !conflict && <ErrorState error={problem} />}
        </aside>
      </div>

      <ConfirmDialog
        open={bypass}
        onOpenChange={setBypass}
        title={t("Apply without approval?")}
        confirmLabel={t("Apply anyway")}
        busy={actions.apply.isPending}
        onConfirm={() => {
          if (!reason.trim()) return;
          setBypass(false);
          void startApply(reason.trim());
        }}
      >
        <p>{t("This bypasses approval. The reason is recorded in the audit log.")}</p>
        <textarea
          aria-label={t("Break-glass reason")}
          className="w-full rounded-md border bg-card p-2 text-sm text-foreground"
          rows={3}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </ConfirmDialog>
    </div>
  );
}
