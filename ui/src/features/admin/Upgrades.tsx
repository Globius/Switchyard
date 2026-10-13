// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Admin > Upgrades (system-admin): what version each part runs, whether to upgrade, where
 * configuration backups go, and where to look for newer releases (ADR 0205). It advises and never
 * blocks: an engine on an old agent keeps receiving configuration.
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

export type Check = components["schemas"]["UpgradeCheckRead"];
export type Finding = components["schemas"]["FindingRead"];
type Run = components["schemas"]["BackupRunRead"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60";

/** The one line at the top: what the reader should do, in the order that matters. */
export function headline(check: Check): { tone: "healthy" | "degraded" | "down"; text: string } {
  if (check.findings.some((f) => f.severity === "error"))
    return { tone: "down", text: t("Something needs fixing before an upgrade") };
  if (check.update)
    return {
      tone: "degraded",
      text: t("Version {v} is available. Upgrading is recommended.", { v: check.update.version }),
    };
  if (check.findings.some((f) => f.severity === "warning"))
    return { tone: "degraded", text: t("Up to date, with recommendations") };
  return { tone: "healthy", text: t("Up to date") };
}

const SEVERITY: Record<Finding["severity"], "down" | "degraded" | "neutral"> = {
  error: "down",
  warning: "degraded",
  info: "neutral",
};

/** Findings, worst first, each with what to do about it. */
export function FindingList({ findings }: { findings: Finding[] }) {
  const order = { error: 0, warning: 1, info: 2 } as const;
  const sorted = [...findings].sort((a, b) => order[a.severity] - order[b.severity]);
  if (sorted.length === 0)
    return <p className="p-4 text-sm text-muted-foreground">{t("Nothing to do.")}</p>;
  return (
    <ul className="divide-y">
      {sorted.map((f, i) => (
        <li key={`${f.code}-${f.subject}-${i}`} className="flex items-start gap-3 px-4 py-3">
          <StatusPill status={SEVERITY[f.severity]}>{t(f.severity)}</StatusPill>
          <div className="space-y-0.5 text-sm">
            <div>
              <span className="font-medium">{f.subject}</span>: {f.message}
            </div>
            {f.action && <div className="text-muted-foreground">{f.action}</div>}
          </div>
        </li>
      ))}
    </ul>
  );
}

function useCheck() {
  return useQuery({
    queryKey: ["admin", "upgrade-check"],
    refetchInterval: 60_000,
    queryFn: async () => unwrap(await api.GET("/api/v1/system/upgrade/check")),
  });
}

function Section({
  title,
  children,
  aside,
}: {
  title: string;
  children: React.ReactNode;
  aside?: React.ReactNode;
}) {
  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <header className="flex items-center gap-3 border-b px-4 py-3">
        <h2 className="text-base font-semibold">{title}</h2>
        <span className="ml-auto flex items-center gap-2">{aside}</span>
      </header>
      {children}
    </section>
  );
}

function Status() {
  const q = useCheck();
  if (q.isPending) return <TableSkeleton rows={3} cols={2} />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const c = q.data;
  const h = headline(c);
  return (
    <Section title={t("Versions")} aside={<StatusPill status={h.tone}>{h.text}</StatusPill>}>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 border-b px-4 py-3 text-sm">
        <dt className="text-muted-foreground">{t("Controller")}</dt>
        <dd>{c.controller_version}</dd>
        <dt className="text-muted-foreground">{t("Latest known")}</dt>
        <dd>{c.latest_version}</dd>
        <dt className="text-muted-foreground">{t("Database")}</dt>
        <dd>
          {c.schema_current ?? "—"} → {c.schema_head} ({c.schema_state})
        </dd>
        <dt className="text-muted-foreground">{t("Engines")}</dt>
        <dd>
          {c.engines.length === 0
            ? t("none reported yet")
            : c.engines.map((e) => `${e.name} ${e.agent_version ?? "?"}`).join(", ")}
        </dd>
      </dl>
      {c.update && (
        <div className="space-y-2 border-b px-4 py-3 text-sm">
          <div className="font-medium">
            {t("Version {v} ({date})", { v: c.update.version, date: c.update.date })}
          </div>
          {c.update.path.length > 1 && (
            <p>{t("Install in this order: {path}", { path: c.update.path.join(" → ") })}</p>
          )}
          {(["breaking", "action_required", "deprecated", "new"] as const).map((g) => {
            const lines = c.update?.notes[g] ?? [];
            return lines.length > 0 ? (
              <div key={g}>
                <div className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                  {t(g.replace("_", " "))}
                </div>
                <ul className="list-disc pl-5">
                  {lines.map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              </div>
            ) : null;
          })}
          {c.update.notes_url && (
            <a
              className="text-primary hover:underline"
              href={c.update.notes_url}
              target="_blank"
              rel="noreferrer noopener"
            >
              {t("Release notes")}
            </a>
          )}
          <p className="text-muted-foreground">
            {t("Back up first, then run the upgrade from a terminal:")}{" "}
            <code>syctl upgrade plan BUNDLE</code>, <code>syctl upgrade apply BUNDLE</code>.
          </p>
        </div>
      )}
      <FindingList findings={c.findings} />
    </Section>
  );
}

function Backups() {
  const qc = useQueryClient();
  const [target, setTarget] = useState("");
  const targets = useQuery({
    queryKey: ["admin", "backup-targets"],
    queryFn: async () => unwrap(await api.GET("/api/v1/system/backup-targets")),
  });
  const runs = useQuery({
    queryKey: ["admin", "backups"],
    refetchInterval: 30_000,
    queryFn: async () => unwrap(await api.GET("/api/v1/system/backups")),
  });
  const chosen = target || targets.data?.[0]?.name || "";
  const run = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/system/backups", {
          body: { target: chosen, reason: "pre-upgrade" },
        }),
      ),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "backups"] });
      void qc.invalidateQueries({ queryKey: ["admin", "upgrade-check"] });
    },
  });
  return (
    <Section
      title={t("Configuration backups")}
      aside={
        <>
          {targets.data && targets.data.length > 1 && (
            <select
              aria-label={t("Backup target")}
              className={INPUT + " w-40"}
              value={chosen}
              onChange={(e) => setTarget(e.target.value)}
            >
              {targets.data.map((x) => (
                <option key={x.name}>{x.name}</option>
              ))}
            </select>
          )}
          <Button size="sm" disabled={!chosen || run.isPending} onClick={() => run.mutate()}>
            {run.isPending ? t("Backing up…") : t("Back up now")}
          </Button>
        </>
      }
    >
      <div className="space-y-2 p-4 text-sm">
        {run.error && <ErrorState error={run.error} />}
        {targets.data?.length === 0 && (
          <p>
            {t(
              "No backup destination yet. Add one with the API (POST /system/backup-targets): local disk, S3-compatible storage, SFTP or FTP.",
            )}
          </p>
        )}
        <p className="text-muted-foreground">
          {t(
            "A backup holds the desired state of every tenant, encrypted. Users, tokens and the audit log come from the database backup.",
          )}
        </p>
      </div>
      {runs.data && runs.data.length > 0 && (
        <table className="w-full text-sm">
          <thead className="border-y bg-muted/40 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-4 py-2">{t("When")}</th>
              <th className="px-4 py-2">{t("Target")}</th>
              <th className="px-4 py-2">{t("Result")}</th>
              <th className="px-4 py-2">{t("Why")}</th>
            </tr>
          </thead>
          <tbody>
            {runs.data.slice(0, 8).map((r: Run) => (
              <tr key={r.id} className="border-b last:border-0">
                <td className="px-4 py-2">{shortWhen(r.created_at)}</td>
                <td className="px-4 py-2">{r.target}</td>
                <td className="px-4 py-2">
                  {r.status === "ok" ? (
                    <StatusPill status="healthy">{t("Written")}</StatusPill>
                  ) : (
                    <StatusPill status="down">{r.error ?? t("Failed")}</StatusPill>
                  )}
                </td>
                <td className="px-4 py-2">{r.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}

function Feed() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["admin", "releases"],
    queryFn: async () => unwrap(await api.GET("/api/v1/system/releases")),
  });
  const [url, setUrl] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: async (v: { enabled: boolean; url: string; channel: "stable" | "candidate" }) =>
      unwrap(
        await api.PUT("/api/v1/system/releases", {
          body: { ...v, trusted_keys: q.data?.trusted_keys ?? {} },
        }),
      ),
    onSuccess: () => {
      setUrl(null);
      void qc.invalidateQueries({ queryKey: ["admin", "releases"] });
    },
  });
  const check = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/system/releases/check")),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "releases"] });
      void qc.invalidateQueries({ queryKey: ["admin", "upgrade-check"] });
    },
  });
  if (q.isPending) return <TableSkeleton rows={2} cols={2} />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const f = q.data;
  const shown = url ?? f.url;
  return (
    <Section
      title={t("Looking for newer releases")}
      aside={
        <Button
          size="sm"
          variant="outline"
          disabled={!f.url || check.isPending}
          onClick={() => check.mutate()}
        >
          {check.isPending ? t("Checking…") : t("Check now")}
        </Button>
      }
    >
      <div className="space-y-3 p-4 text-sm">
        {(save.error || check.error) && <ErrorState error={save.error ?? check.error} />}
        <div className="flex items-start gap-3">
          <Switch
            aria-label={t("Check for new releases")}
            checked={f.enabled}
            disabled={save.isPending || (!f.enabled && !shown)}
            onCheckedChange={(on) => save.mutate({ enabled: on, url: shown, channel: f.channel })}
          />
          <p className="text-muted-foreground">
            {t(
              "Off by default. The controller makes one HTTPS request a day to the address below and uses the answer only if its signature verifies. Nothing about your installation is sent.",
            )}
          </p>
        </div>
        <label className="block space-y-1">
          <span className="font-medium">{t("Feed address")}</span>
          <input
            className={INPUT}
            value={shown}
            placeholder="https://git.example.com/…/releases.json"
            onChange={(e) => setUrl(e.target.value)}
          />
        </label>
        <label className="block space-y-1">
          <span className="font-medium">{t("Channel")}</span>
          <select
            className={INPUT + " w-40"}
            value={f.channel}
            onChange={(e) =>
              save.mutate({
                enabled: f.enabled,
                url: shown,
                channel: e.target.value as "stable" | "candidate",
              })
            }
          >
            <option value="stable">stable</option>
            <option value="candidate">candidate</option>
          </select>
        </label>
        {url !== null && url !== f.url && (
          <Button
            size="sm"
            disabled={save.isPending}
            onClick={() => save.mutate({ enabled: f.enabled, url, channel: f.channel })}
          >
            {t("Save address")}
          </Button>
        )}
        <p className="text-xs text-muted-foreground">
          {f.checked_at
            ? t("Last checked {when}.", { when: shortWhen(f.checked_at) })
            : t("Not checked yet.")}{" "}
          {f.error && <span className="text-status-down">{f.error}</span>}
        </p>
      </div>
    </Section>
  );
}

export function Upgrades() {
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">{t("Upgrades")}</h1>
        <p className="text-sm text-muted-foreground">
          {t("This page advises. An engine on an older version keeps receiving configuration.")}
        </p>
      </div>
      <Status />
      <Backups />
      <Feed />
    </div>
  );
}
