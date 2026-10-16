// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Admin > System settings > Git mirror: where every applied revision is written as YAML (ADR 0216).
 * The token is an OpenBao `secret_ref`; the form never takes a token value.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { StatusPill } from "@/components/sy/StatusPill";
import { ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { shortWhen } from "@/lib/format";
import { t } from "@/lib/i18n";

type Mirror = components["schemas"]["GitMirrorRead"];
type Draft = Pick<Mirror, "url" | "project" | "branch" | "token_ref">;

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60";

/** The first problem with the form, or `null`. A token pasted into the reference is caught here. */
export function mirrorProblem(d: Draft, enabled: boolean): string | null {
  if (d.url && !/^https:\/\/[^\s/@]+(\/\S*)?$/.test(d.url))
    return t("The address must start with https:// and contain no credentials.");
  if (d.token_ref && !/^[A-Za-z0-9._/:-]{1,200}$/.test(d.token_ref))
    return t("The token reference looks like a path such as kv/git/mirror-token, not a token.");
  if (enabled && (!d.url || !d.project || !d.token_ref))
    return t("Address, project and token reference are needed to turn the mirror on.");
  return null;
}

/** The Git mirror card: settings, Test, and how the last write went. */
export function GitMirror() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["admin", "git-mirror"],
    queryFn: async () => unwrap(await api.GET("/api/v1/system/git-mirror")),
  });
  const [draft, setDraft] = useState<Draft | null>(null);
  const [problem, setProblem] = useState("");
  const save = useMutation({
    mutationFn: async (v: Draft & { enabled: boolean }) =>
      unwrap(await api.PUT("/api/v1/system/git-mirror", { body: v })),
    onSuccess: () => {
      setDraft(null);
      void qc.invalidateQueries({ queryKey: ["admin", "git-mirror"] });
    },
  });
  const test = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/system/git-mirror/test")),
  });
  if (q.isPending) return <TableSkeleton rows={2} cols={2} />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const m = q.data;
  const shown: Draft = draft ?? {
    url: m.url,
    project: m.project,
    branch: m.branch,
    token_ref: m.token_ref,
  };
  const edit = (k: keyof Draft, v: string) => setDraft({ ...shown, [k]: v });
  const submit = (enabled: boolean) => {
    const p = mirrorProblem(shown, enabled);
    setProblem(p ?? "");
    if (!p) save.mutate({ ...shown, enabled });
  };
  return (
    <section id="git-mirror" className="overflow-hidden rounded-lg border bg-card">
      <header className="flex items-center gap-3 border-b px-4 py-3">
        <h2 className="text-base font-semibold">{t("Git mirror")}</h2>
        <StatusPill status={m.enabled ? "healthy" : "neutral"}>
          {m.enabled ? t("On") : t("Off")}
        </StatusPill>
        <Button
          size="sm"
          variant="outline"
          className="ml-auto"
          disabled={test.isPending || m.source === "none"}
          onClick={() => test.mutate()}
        >
          {test.isPending ? t("Testing…") : t("Test")}
        </Button>
      </header>
      <div className="space-y-3 p-4 text-sm">
        {(save.error || test.error) && <ErrorState error={save.error ?? test.error} />}
        <p className="text-muted-foreground">
          {t(
            "Every applied revision is also written as YAML to a GitLab repository, for review and recovery. PostgreSQL stays the source of truth: a mirror failure never blocks an apply. Enter an OpenBao secret reference for the token (a project access token with write access to that repository only), never the token itself.",
          )}
        </p>
        {m.source === "environment" && (
          <p role="note" className="rounded-md bg-muted p-3">
            {t(
              "These values come from SWITCHYARD_GITLAB_* on the controller. Saving here replaces them; the token then comes from the reference below.",
            )}
          </p>
        )}
        <div className="flex items-center gap-3">
          <Switch
            aria-label={t("Mirror revisions to Git")}
            checked={m.enabled}
            disabled={save.isPending || (!m.enabled && m.source === "none" && draft === null)}
            onCheckedChange={(on) => submit(on)}
          />
          <span className="font-medium">{t("Mirror revisions to Git")}</span>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block space-y-1">
            <span className="font-medium">{t("GitLab address")}</span>
            <input
              className={INPUT}
              placeholder="https://git.example.com"
              value={shown.url}
              onChange={(e) => edit("url", e.target.value)}
            />
          </label>
          <label className="block space-y-1">
            <span className="font-medium">{t("Project")}</span>
            <input
              className={INPUT}
              placeholder="group/switchyard-mirror"
              value={shown.project}
              onChange={(e) => edit("project", e.target.value)}
            />
          </label>
          <label className="block space-y-1">
            <span className="font-medium">{t("Branch")}</span>
            <input
              className={INPUT}
              value={shown.branch}
              onChange={(e) => edit("branch", e.target.value)}
            />
          </label>
          <label className="block space-y-1">
            <span className="font-medium">{t("Token reference")}</span>
            <input
              className={INPUT}
              placeholder="kv/git/mirror-token"
              value={shown.token_ref}
              onChange={(e) => edit("token_ref", e.target.value)}
            />
          </label>
        </div>
        {problem && (
          <p role="alert" className="text-status-down">
            {problem}
          </p>
        )}
        {draft !== null && (
          <div className="flex gap-2">
            <Button size="sm" disabled={save.isPending} onClick={() => submit(m.enabled)}>
              {save.isPending ? t("Saving…") : t("Save")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                setDraft(null);
                setProblem("");
              }}
            >
              {t("Cancel")}
            </Button>
          </div>
        )}
        {test.data && (
          <StatusPill status={test.data.ok ? "healthy" : "down"}>
            {test.data.ok
              ? t("Reachable. Branch head {sha}.", { sha: (test.data.head ?? "").slice(0, 8) })
              : `${t("Failed")}: ${test.data.error ?? ""}`}
          </StatusPill>
        )}
        <p className="text-xs text-muted-foreground">
          {m.last_ok_at
            ? t("Last written {when} ({sha}).", {
                when: shortWhen(m.last_ok_at),
                sha: (m.last_sha ?? "").slice(0, 8),
              })
            : t("Nothing written yet.")}{" "}
          {m.last_error && <span className="text-status-down">{m.last_error}</span>}
        </p>
      </div>
    </section>
  );
}
