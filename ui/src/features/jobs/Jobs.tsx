// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Jobs: applies, deployments, SDN and certificate work, newest first, with stages, log, retry and cancel. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { JobProgress } from "@/features/review/JobProgress";
import { phaseOf, type Job, type Phase } from "@/features/review/model";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const KINDS: Record<string, string> = {
  apply_change_set: "Apply",
  deploy_se_group: "Deploy SE group",
  retire_engine: "Retire engine",
  apply_sdn: "Apply SDN",
  issue_cert: "Issue certificate",
  renew_certs: "Renew certificates",
  sync_cloudflare_ips: "Sync Cloudflare IPs",
  poll_status: "Poll status",
  git_mirror: "Git mirror",
  gc: "Garbage collection",
  evaluate_alerts: "Evaluate alerts",
};
const STATES = ["queued", "running", "succeeded", "failed", "dead", "cancelled"] as const;

type State = components["schemas"]["JobState"];
type Kind = components["schemas"]["JobKind"];

function useJobs(tenant: string, state: string, kind: string) {
  return useQuery({
    queryKey: ["t", tenant, "jobs", { state, kind }],
    refetchInterval: 5000,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/jobs", {
          params: {
            path: { t: tenant },
            query: {
              limit: 100,
              ...(state ? { state: state as State } : {}),
              ...(kind ? { kind: kind as Kind } : {}),
            },
          },
        }),
      ),
  });
}

function useJobLog(tenant: string, id: string) {
  return useQuery({
    queryKey: ["t", tenant, "jobs", id, "log"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/jobs/{job_id}/log", {
          params: { path: { t: tenant, job_id: id } },
        }),
      ),
  });
}

/** Job state in the handoff's vocabulary: Running, Retrying after error, Reverted, Gave up. */
export function JobPill({ job }: { job: Job }) {
  const phase: Phase = phaseOf("draft", job);
  if (phase === "gave_up") return <StatusPill status="down">{t("Dead · gave up")}</StatusPill>;
  if (phase === "retrying")
    return <StatusPill status="degraded">{t("Retrying after error")}</StatusPill>;
  if (phase === "reverted") return <StatusPill status="down">{t("Reverted at canary")}</StatusPill>;
  if (phase === "failed") {
    const stage = (job.stages ?? []).find((x) => x.state === "failed")?.name;
    return (
      <StatusPill status="down">
        {stage ? t("Failed at {stage}", { stage }) : t("Failed")}
      </StatusPill>
    );
  }
  switch (job.state) {
    case "running": {
      const stage = (job.stages ?? []).find((s) => s.state === "running");
      return (
        <StatusPill status="pending">
          {t("Running")}
          {stage ? ` · ${stage.name}` : ""}
        </StatusPill>
      );
    }
    case "queued":
      return <StatusPill status="pending">{t("Queued")}</StatusPill>;
    case "succeeded":
      return <StatusPill status="healthy">{t("Succeeded")}</StatusPill>;
    case "failed":
      return <StatusPill status="down">{t("Failed")}</StatusPill>;
    default:
      return <StatusPill status="neutral">{t("Cancelled")}</StatusPill>;
  }
}

/** `4 s`, `1 min 4 s` or `12 min` from when the job started to when it ended (or now). */
export function duration(j: Pick<Job, "started_at" | "finished_at">, now = Date.now()): string {
  if (!j.started_at) return "—";
  const secs = Math.max(
    0,
    Math.round(
      ((j.finished_at ? Date.parse(j.finished_at) : now) - Date.parse(j.started_at)) / 1000,
    ),
  );
  if (secs < 60) return `${secs} s`;
  const m = Math.floor(secs / 60);
  const r = secs % 60;
  return m >= 10 || r === 0 ? `${m} min` : `${m} min ${r} s`;
}

function jobTitle(j: Job): string {
  return t(KINDS[j.kind] ?? j.kind);
}

function Detail({ tenant, job }: { tenant: string; job: Job }) {
  const log = useJobLog(tenant, job.id);
  const qc = useQueryClient();
  const can = useCan(tenant);
  const retry = can("retry", "job");
  const cancel = can("cancel", "job");
  const ownCancel = job.kind === "apply_change_set" && can("cancel", "job_own").allowed;
  const act = useMutation({
    mutationFn: async (what: "retry" | "cancel") =>
      unwrap(
        what === "retry"
          ? await api.POST("/api/v1/tenants/{t}/jobs/{job_id}/retry", {
              params: { path: { t: tenant, job_id: job.id } },
            })
          : await api.POST("/api/v1/tenants/{t}/jobs/{job_id}/cancel", {
              params: { path: { t: tenant, job_id: job.id } },
            }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["t", tenant] }),
  });
  const retryable = job.state === "failed" || job.state === "dead";
  const cancellable = job.state === "queued";
  return (
    <aside className="rounded-lg border bg-card" aria-label={t("Job details")}>
      <header className="flex items-center gap-2 border-b px-4 py-3">
        <h2 className="text-base font-semibold">{jobTitle(job)}</h2>
        <JobPill job={job} />
      </header>
      <div className="space-y-4 p-4 text-sm">
        <dl className="grid grid-cols-[8rem_1fr] gap-y-2">
          {job.change_set_id && (
            <>
              <dt className="text-muted-foreground">{t("Change set")}</dt>
              <dd>
                <Link
                  to="/t/$tenant/review/$cs"
                  params={{ tenant, cs: job.change_set_id }}
                  className="font-mono text-xs text-primary hover:underline"
                >
                  {job.change_set_id.slice(0, 8)}…
                </Link>
              </dd>
            </>
          )}
          <dt className="text-muted-foreground">{t("Created")}</dt>
          <dd>
            {new Date(job.created_at).toLocaleTimeString()}
            {job.started_at &&
              ` · ${t("started {time}", { time: new Date(job.started_at).toLocaleTimeString() })}`}
          </dd>
          <dt className="text-muted-foreground">{t("Attempt")}</dt>
          <dd>{t("{n} of {max}", { n: job.attempts, max: job.max_attempts })}</dd>
          {job.error && (
            <>
              <dt className="text-muted-foreground">{t("Error")}</dt>
              <dd className="text-status-down">{job.error}</dd>
            </>
          )}
        </dl>
        {(job.stages?.length ?? 0) > 0 && (
          <div>
            <div className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {t("Stages")}
            </div>
            <JobProgress stages={job.stages} />
          </div>
        )}
        <div>
          <div className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("Log")}
          </div>
          {log.isPending ? (
            <TableSkeleton rows={2} cols={1} />
          ) : log.isError ? (
            <ErrorState error={log.error} onRetry={() => void log.refetch()} />
          ) : log.data.lines.length === 0 ? (
            <p className="text-muted-foreground">{t("No log lines yet.")}</p>
          ) : (
            <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 font-mono text-xs">
              {log.data.lines
                .map(
                  (l) => `${new Date(l.at).toLocaleTimeString([], { hour12: false })} ${l.message}`,
                )
                .join("\n")}
            </pre>
          )}
        </div>
        {act.error && <ErrorState error={act.error} />}
        <div className="flex gap-2">
          {retryable && (
            <Tip label={retry.reason}>
              <Button
                variant="outline"
                disabled={!retry.allowed || act.isPending}
                onClick={() => act.mutate("retry")}
              >
                {t("Retry job")}
              </Button>
            </Tip>
          )}
          {cancellable && (
            <Tip label={cancel.reason}>
              <Button
                variant="outline"
                disabled={!(cancel.allowed || ownCancel) || act.isPending}
                onClick={() => act.mutate("cancel")}
              >
                {t("Cancel job")}
              </Button>
            </Tip>
          )}
        </div>
        <p className="text-xs text-muted-foreground">
          {t(
            "A running job is never interrupted mid-rollout. Only queued jobs can be cancelled; cancelling an apply marks its change set failed so it can be applied again.",
          )}
        </p>
      </div>
    </aside>
  );
}

export function Jobs({ tenant }: { tenant: string }) {
  const [state, setState] = useState("");
  const [kind, setKind] = useState("");
  const [picked, setPicked] = useState<string | null>(null);
  const list = useJobs(tenant, state, kind);
  const items = list.data?.items ?? [];
  const selected = items.find((j) => j.id === picked) ?? items[0];
  const SELECT =
    "h-9 rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">{t("Jobs")}</h1>
        <p className="text-sm text-muted-foreground">
          {t("Applies, deployments, SDN changes and certificate work, newest first")}
        </p>
      </div>
      <div className="flex gap-3">
        <select
          aria-label={t("State")}
          className={cn(SELECT, "w-48")}
          value={state}
          onChange={(e) => setState(e.target.value)}
        >
          <option value="">{t("All states")}</option>
          {STATES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select
          aria-label={t("Kind")}
          className={cn(SELECT, "w-52")}
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        >
          <option value="">{t("All kinds")}</option>
          {Object.entries(KINDS).map(([k, label]) => (
            <option key={k} value={k}>
              {t(label)}
            </option>
          ))}
        </select>
      </div>
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_26rem]">
        <div className="overflow-hidden rounded-lg border bg-card">
          {list.isPending ? (
            <TableSkeleton cols={3} />
          ) : list.isError ? (
            <ErrorState error={list.error} onRetry={() => void list.refetch()} />
          ) : items.length === 0 ? (
            <EmptyState
              title={t("No jobs")}
              body={t("Applies, deployments and certificate work appear here.")}
            />
          ) : (
            <table className="w-full text-[13px]">
              <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
                <tr>
                  {["Job", "State", "Attempts", "Duration"].map((h) => (
                    <th key={h} className="px-4 py-2.5 font-medium">
                      {t(h)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {items.map((j) => (
                  <tr key={j.id} className={cn(selected?.id === j.id && "bg-accent/40")}>
                    <td className="px-4 py-3">
                      <button
                        type="button"
                        className="text-left font-medium text-primary hover:underline"
                        aria-current={selected?.id === j.id}
                        onClick={() => setPicked(j.id)}
                      >
                        {jobTitle(j)}
                      </button>
                      {j.change_set_id && (
                        <Link
                          to="/t/$tenant/review/$cs"
                          params={{ tenant, cs: j.change_set_id }}
                          className="font-mono text-xs text-primary hover:underline"
                        >
                          {t("change set {id}", { id: `${j.change_set_id.slice(0, 8)}…` })}
                        </Link>
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <JobPill job={j} />
                    </td>
                    <td className="px-4 py-3">
                      {t("{n} of {max}", { n: j.attempts, max: j.max_attempts })}
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">{duration(j)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        {selected && <Detail key={selected.id} tenant={tenant} job={selected} />}
      </div>
      <p className="text-xs text-muted-foreground">
        {t(
          "“Reverted” means the job finished and put the old config back (the change was rejected by the canary). “Retrying after error” means an infrastructure error, retried on its own. Operators can retry or cancel only their own applies; deployment and SDN jobs need tenant-admin.",
        )}
      </p>
    </div>
  );
}
