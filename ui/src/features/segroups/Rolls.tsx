// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Engine rolls: replace every engine of an SE group with one from a new template, one at a time
 * (ADR 0205). A roll that fails or takes too long pauses with the old and the new engine both
 * in place; this page resumes or aborts it. Starting one needs tenant-admin.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { shortWhen } from "@/lib/format";
import { t } from "@/lib/i18n";

export type Roll = components["schemas"]["RollRead"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60";

const PHASE_LABEL: Record<string, string> = {
  pending: "Waiting",
  deploying: "Deploying replacement",
  awaiting_enrollment: "Waiting for it to enroll",
  reapplying: "Applying configuration",
  settling: "Checking health",
  retiring: "Retiring old engine",
  settling_after: "Checking health again",
  done: "Replaced",
};

/** Plain words for a phase; unknown phases show as they came. */
export function phaseLabel(phase: string): string {
  return t(PHASE_LABEL[phase] ?? phase);
}

/** One line for the roll: how far it is and what it is doing. */
export function summary(r: Roll): string {
  const done = r.steps.filter((s) => s.phase === "done").length;
  const active = r.steps.find((s) => s.phase !== "done");
  const head = t("{done} of {total} engines replaced", { done, total: r.steps.length });
  if (r.status === "done") return head;
  if (r.holding) return `${head}. ${r.reason}`;
  if (r.status === "paused") return `${head}. ${r.reason}`;
  return active ? `${head}. ${active.old}: ${phaseLabel(active.phase)}` : head;
}

const TONE: Record<Roll["status"], "healthy" | "degraded" | "down" | "pending"> = {
  running: "pending",
  paused: "degraded",
  aborted: "down",
  done: "healthy",
};

const LABEL: Record<Roll["status"], string> = {
  running: "Running",
  paused: "Paused",
  aborted: "Aborted",
  done: "Done",
};

function useRolls(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "rolls"],
    refetchInterval: 5_000,
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/rolls", { params: { path: { t: tenant } } })),
  });
}

function useGroupList(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "se-groups", "for-rolls"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/se-groups", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
}

interface Form {
  group: string;
  template: string;
  start: string;
  end: string;
  after: string;
  soak: string;
}

const EMPTY: Form = { group: "", template: "", start: "", end: "", after: "", soak: "0" };

/** The request body for the form, or the first problem in words. */
export function rollBody(
  f: Form,
): { error: string } | { body: components["schemas"]["RollStartIn"] } {
  const template = Number(f.template);
  if (!Number.isInteger(template) || template < 100)
    return { error: t("Give the Proxmox template ID (a number from 100).") };
  if (Boolean(f.start) !== Boolean(f.end))
    return { error: t("Give both ends of the window, or neither.") };
  const soak = Number(f.soak || "0");
  if (!Number.isInteger(soak) || soak < 0 || soak > 86_400)
    return { error: t("Soak time is whole seconds, up to a day.") };
  return {
    body: {
      template_vmid: template,
      window_start: f.start || null,
      window_end: f.end || null,
      start_after: f.after ? new Date(f.after).toISOString() : null,
      soak_seconds: soak,
    },
  };
}

function StartSheet({ tenant, onClose }: { tenant: string; onClose: () => void }) {
  const qc = useQueryClient();
  const groups = useGroupList(tenant);
  const [f, setF] = useState<Form>(EMPTY);
  const [problem, setProblem] = useState("");
  const set = (p: Partial<Form>) => setF({ ...f, ...p });
  const group = f.group || groups.data?.items[0]?.id || "";
  const start = useMutation({
    mutationFn: async (body: components["schemas"]["RollStartIn"]) =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/se-groups/{group}/rolls", {
          params: { path: { t: tenant, group } },
          body,
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["t", tenant, "rolls"] });
      onClose();
    },
  });
  const submit = () => {
    const r = rollBody({ ...f, group });
    if ("error" in r) return setProblem(r.error);
    setProblem("");
    start.mutate(r.body);
  };
  return (
    <Sheet
      open
      centered
      onOpenChange={(o) => !o && onClose()}
      title={t("Roll engines to a new template")}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button disabled={start.isPending || !group} onClick={submit}>
            {start.isPending ? t("Starting…") : t("Start roll")}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <p className="text-sm text-muted-foreground">
          {t(
            "Replaces every enabled engine of the group, one at a time. Each replacement must be healthy before the old engine is retired.",
          )}
        </p>
        {(problem || start.error) && (
          <ErrorState error={problem ? new Error(problem) : start.error} />
        )}
        <label className="block space-y-1 text-sm">
          <span className="font-medium">{t("SE group")}</span>
          <select className={INPUT} value={group} onChange={(e) => set({ group: e.target.value })}>
            {groups.data?.items.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </label>
        <label className="block space-y-1 text-sm">
          <span className="font-medium">{t("Template ID")}</span>
          <input
            className={INPUT}
            inputMode="numeric"
            value={f.template}
            onChange={(e) => set({ template: e.target.value })}
            placeholder="9770"
          />
        </label>
        <fieldset className="space-y-2 text-sm">
          <legend className="font-medium">{t("When (all optional, times are UTC)")}</legend>
          <div className="grid grid-cols-2 gap-3">
            <label className="space-y-1">
              <span>{t("Window opens")}</span>
              <input
                className={INPUT}
                type="time"
                value={f.start}
                onChange={(e) => set({ start: e.target.value })}
              />
            </label>
            <label className="space-y-1">
              <span>{t("Window closes")}</span>
              <input
                className={INPUT}
                type="time"
                value={f.end}
                onChange={(e) => set({ end: e.target.value })}
              />
            </label>
          </div>
          <p className="text-muted-foreground">
            {t(
              "A new engine starts only inside the window. One already in progress finishes first.",
            )}
          </p>
          <label className="block space-y-1">
            <span>{t("Do not start before")}</span>
            <input
              className={INPUT}
              type="datetime-local"
              value={f.after}
              onChange={(e) => set({ after: e.target.value })}
            />
          </label>
          <label className="block space-y-1">
            <span>{t("Wait after each engine (seconds)")}</span>
            <input
              className={INPUT}
              inputMode="numeric"
              value={f.soak}
              onChange={(e) => set({ soak: e.target.value })}
            />
          </label>
        </fieldset>
      </div>
    </Sheet>
  );
}

export function Rolls({ tenant }: { tenant: string }) {
  const qc = useQueryClient();
  const can = useCan(tenant);
  const manage = can("deploy", "se_group").allowed;
  const q = useRolls(tenant);
  const groups = useGroupList(tenant);
  const [sheet, setSheet] = useState(false);
  const [abort, setAbort] = useState<Roll | null>(null);
  const control = useMutation({
    mutationFn: async (v: { roll: Roll; action: "pause" | "resume" | "abort" }) =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/rolls/{roll}", {
          params: { path: { t: tenant, roll: v.roll.id } },
          body: { action: v.action },
        }),
      ),
    onSettled: () => {
      setAbort(null);
      void qc.invalidateQueries({ queryKey: ["t", tenant, "rolls"] });
    },
  });
  const groupName = (id: string) =>
    groups.data?.items.find((g) => g.id === id)?.name ?? id.slice(0, 8);
  return (
    <div className="space-y-5">
      <div className="flex items-start gap-3">
        <div>
          <h1 className="text-2xl font-semibold">{t("Engine rolls")}</h1>
          <p className="text-sm text-muted-foreground">
            {t(
              "Replace the engines of an SE group with ones from a new template, one at a time. A problem pauses the roll with the old engine still serving.",
            )}
          </p>
        </div>
        {manage && (
          <Button className="ml-auto" onClick={() => setSheet(true)}>
            {t("Roll engines…")}
          </Button>
        )}
      </div>
      {control.error && <ErrorState error={control.error} />}
      {q.isPending ? (
        <TableSkeleton rows={3} cols={4} />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : q.data.length === 0 ? (
        <EmptyState
          title={t("No rolls yet")}
          body={t("Build a new SE template, then roll a group to it.")}
        />
      ) : (
        <ul className="space-y-3">
          {q.data.map((r) => (
            <li key={r.id} className="overflow-hidden rounded-lg border bg-card">
              <div className="flex items-center gap-3 border-b px-4 py-3">
                <h2 className="text-base font-semibold">{groupName(r.se_group_id)}</h2>
                <StatusPill status={TONE[r.status]}>{t(LABEL[r.status])}</StatusPill>
                <span className="text-sm text-muted-foreground">
                  {t("template {id}", { id: r.template_vmid })}
                  {r.target_version ? ` · ${r.target_version}` : ""}
                  {r.window ? ` · ${r.window}` : ""}
                  {r.soak_seconds > 0 ? ` · ${t("{s}s apart", { s: r.soak_seconds })}` : ""}
                </span>
                <span className="ml-auto text-xs text-muted-foreground">
                  {shortWhen(r.created_at)}
                </span>
              </div>
              <p className="px-4 pt-3 text-sm">{summary(r)}</p>
              <ol className="space-y-1 px-4 py-3 text-sm">
                {r.steps.map((s) => (
                  <li key={s.old} className="flex gap-3">
                    <span className="w-28 font-medium">{s.old}</span>
                    <span className="text-muted-foreground">
                      {s.new ? `→ ${s.new} · ` : ""}
                      {phaseLabel(s.phase)}
                    </span>
                  </li>
                ))}
              </ol>
              {manage && (r.status === "running" || r.status === "paused") && (
                <div className="flex gap-2 border-t px-4 py-3">
                  {r.status === "paused" ? (
                    <Button
                      size="sm"
                      disabled={control.isPending}
                      onClick={() => control.mutate({ roll: r, action: "resume" })}
                    >
                      {t("Resume")}
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={control.isPending}
                      onClick={() => control.mutate({ roll: r, action: "pause" })}
                    >
                      {t("Pause")}
                    </Button>
                  )}
                  <Button size="sm" variant="outline" onClick={() => setAbort(r)}>
                    {t("Abort")}
                  </Button>
                </div>
              )}
              <details className="border-t px-4 py-2 text-xs text-muted-foreground">
                <summary className="cursor-pointer">{t("Log")}</summary>
                <ul className="mt-2 space-y-0.5">
                  {r.log.map((line, i) => (
                    <li key={`${i}-${line}`}>{line}</li>
                  ))}
                </ul>
              </details>
            </li>
          ))}
        </ul>
      )}
      {sheet && <StartSheet tenant={tenant} onClose={() => setSheet(false)} />}
      <ConfirmDialog
        open={abort !== null}
        onOpenChange={(o) => !o && setAbort(null)}
        title={t("Abort this roll?")}
        confirmLabel={t("Abort roll")}
        busy={control.isPending}
        onConfirm={() => abort && control.mutate({ roll: abort, action: "abort" })}
      >
        <p>
          {t(
            "Engines already replaced stay replaced. The engine being replaced keeps serving, and a replacement that was already created stays until you retire it.",
          )}
        </p>
      </ConfirmDialog>
    </div>
  );
}
