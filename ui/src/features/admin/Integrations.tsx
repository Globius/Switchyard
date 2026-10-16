// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Admin > Integrations (system-admin): outside systems the controller talks to. Today CrowdSec
 * (ADR 0213): the local API address, the bouncer key (write-only, kept in OpenBao), which decision
 * origins count, a connection test, the status of the last read and "Sync now".
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { StatusPill } from "@/components/sy/StatusPill";
import { ErrorState, TableSkeleton } from "@/components/sy/states";
import { WriteOnlySecret } from "@/components/sy/WriteOnlySecret";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { shortWhen } from "@/lib/format";
import { t } from "@/lib/i18n";

type CrowdSec = components["schemas"]["CrowdSecRead"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60";

/** `"a, b ,,c"` as `["a", "b", "c"]`. */
export function parseOrigins(text: string): string[] {
  return text
    .split(",")
    .map((o) => o.trim())
    .filter(Boolean);
}

/** The pill next to the title: whether CrowdSec is set up and whether the last read worked. */
export function crowdSecStatus(c: CrowdSec): {
  tone: "healthy" | "down" | "neutral";
  text: string;
} {
  if (!c.configured) return { tone: "neutral", text: t("Not set up") };
  if (c.ok === false) return { tone: "down", text: t("Last sync failed") };
  if (c.ok === true) return { tone: "healthy", text: t("Working") };
  return { tone: "neutral", text: t("Not synced yet") };
}

/** What an administrator runs or sets on the CrowdSec engine; shown so it can be copied (nothing here is a secret). */
export function setupSnippet(c: CrowdSec): string {
  const lines = [
    "# deploy/crowdsec/crowdsec.env on the CrowdSec host",
    "CROWDSEC_BIND=<a private address of the host>",
    "CROWDSEC_BOUNCER_KEY=<the shared bouncer key, from OpenBao>",
    "CROWDSEC_DISABLE_ONLINE_API=true   # sharing with CrowdSec stays off until you decide",
    "",
    "# in deploy/crowdsec: docker compose --env-file crowdsec.env up -d",
  ];
  if (c.per_engine_keys) lines.push("# for each engine: ./register-engine.sh add <engine-name>");
  if (c.url) lines.push("", `# Switchyard talks to ${c.url}`);
  return lines.join("\n");
}
function CrowdSecCard() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["admin", "crowdsec"],
    queryFn: async () => unwrap(await api.GET("/api/v1/system/crowdsec")),
  });
  const [url, setUrl] = useState<string | null>(null);
  const [origins, setOrigins] = useState<string | null>(null);
  const [logs, setLogs] = useState<string | null>(null);
  const [perEngine, setPerEngine] = useState<boolean | null>(null);
  const [key, setKey] = useState("");
  const refresh = () => void qc.invalidateQueries({ queryKey: ["admin", "crowdsec"] });
  const save = useMutation({
    mutationFn: async (c: CrowdSec) =>
      unwrap(
        await api.PUT("/api/v1/system/crowdsec", {
          body: {
            url: url ?? c.url,
            origins: parseOrigins(origins ?? c.origins.join(", ")),
            logs_address: logs ?? c.logs_address,
            per_engine_keys: perEngine ?? c.per_engine_keys,
            key_ref: c.key_ref,
            ...(key ? { api_key: key } : {}),
          },
        }),
      ),
    onSuccess: () => {
      setUrl(null);
      setOrigins(null);
      setLogs(null);
      setPerEngine(null);
      setKey("");
      refresh();
    },
  });
  const test = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/system/crowdsec/test")),
  });
  const sync = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/system/crowdsec/sync")),
    onSettled: refresh,
  });
  if (q.isPending) return <TableSkeleton rows={3} cols={2} />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const c = q.data;
  const status = crowdSecStatus(c);
  const dirty =
    url !== null || origins !== null || logs !== null || perEngine !== null || key !== "";
  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <header className="flex items-center gap-3 border-b px-4 py-3">
        <h2 className="text-base font-semibold">CrowdSec</h2>
        <StatusPill status={status.tone}>{status.text}</StatusPill>
        <span className="ml-auto flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={!c.configured || dirty || test.isPending}
            onClick={() => test.mutate()}
          >
            {test.isPending ? t("Testing…") : t("Test connection")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!c.configured || dirty || sync.isPending}
            onClick={() => sync.mutate()}
          >
            {t("Sync now")}
          </Button>
        </span>
      </header>
      <div className="space-y-4 p-4 text-sm">
        {(save.error || test.error || sync.error) && (
          <ErrorState error={save.error ?? test.error ?? sync.error} />
        )}
        <p className="text-muted-foreground">
          {t(
            "Bans from your CrowdSec engine become the reputation feed “crowdsec”. Give a policy set managed by reputation the feed crowdsec and attach it to a service as its deny list. The controller reads the decisions; engines never contact CrowdSec.",
          )}
        </p>
        {c.source === "environment" && (
          <p role="note" className="rounded-md bg-muted p-3">
            {t(
              "These settings come from the environment (SWITCHYARD_CROWDSEC_*). Saving here stores them in the database, which then takes precedence.",
            )}
          </p>
        )}
        <label className="block space-y-1">
          <span className="font-medium">{t("Local API address")}</span>
          <input
            className={INPUT}
            value={url ?? c.url}
            placeholder="https://crowdsec.example.com:8080"
            onChange={(e) => setUrl(e.target.value)}
          />
        </label>
        <WriteOnlySecret
          label={t("Bouncer key")}
          isSet={Boolean(c.key_ref)}
          value={key}
          onChange={setKey}
          help={t(
            "Register a bouncer on your CrowdSec engine with cscli bouncers add and paste its key.",
          )}
        />
        {c.key_ref && (
          <p className="text-xs text-muted-foreground">
            {t("Stored at")} <code className="font-mono">{c.key_ref}</code>
          </p>
        )}
        <label className="block space-y-1">
          <span className="font-medium">{t("Origins to use")}</span>
          <input
            className={INPUT}
            value={origins ?? c.origins.join(", ")}
            placeholder="crowdsec, CAPI"
            onChange={(e) => setOrigins(e.target.value)}
          />
          <span className="text-xs text-muted-foreground">
            {t(
              "Comma separated: crowdsec (your own detections), CAPI (the community blocklist), cscli, lists. Empty uses all.",
            )}
          </span>
        </label>
        <label className="block space-y-1">
          <span className="font-medium">{t("Access log address (UDP)")}</span>
          <input
            className={INPUT}
            value={logs ?? c.logs_address}
            placeholder="crowdsec.example.com:514"
            onChange={(e) => setLogs(e.target.value)}
          />
          <span className="text-xs text-muted-foreground">
            {t(
              "Where engines send the access logs of services whose CrowdSec policy has “Send access logs” on: the syslog datasource of your CrowdSec engine. UDP is not encrypted, so use a private network or a tunnel. Empty turns the feed off.",
            )}
          </span>
        </label>
        <label className="flex items-start gap-3">
          <Switch
            aria-label={t("One key per engine")}
            checked={perEngine ?? c.per_engine_keys}
            onCheckedChange={setPerEngine}
          />
          <span>
            <span className="font-medium">{t("One key per engine")}</span>
            <span className="block text-xs text-muted-foreground">
              {t(
                "Each engine uses its own bouncer key, so one engine can be rotated or revoked without touching the others. CrowdSec cannot create bouncers remotely: register each engine with deploy/crowdsec/register-engine.sh (below). An engine without its key fails its apply.",
              )}
            </span>
          </span>
        </label>
        <div className="flex items-center gap-2">
          <Button disabled={!dirty || save.isPending} onClick={() => save.mutate(c)}>
            {save.isPending ? t("Saving…") : t("Save")}
          </Button>
        </div>
        {test.data && (
          <p role="status" className={test.data.ok ? "text-foreground" : "text-destructive"}>
            {test.data.ok
              ? t("Connection works: {n} networks are banned right now.", {
                  n: test.data.bans ?? 0,
                })
              : t("Connection failed: {error}", { error: test.data.error ?? "" })}
          </p>
        )}
        {sync.data && (
          <p role="status" className="text-muted-foreground">
            {t("Sync queued. The lists change through a change set from system:iplists.")}
          </p>
        )}
        <details className="rounded-md border p-3">
          <summary className="cursor-pointer font-medium">
            {t("Set up the CrowdSec engine")}
          </summary>
          <pre
            aria-label={t("Setup commands")}
            className="mt-2 overflow-x-auto rounded bg-muted p-3 font-mono text-xs"
          >
            {setupSnippet(c)}
          </pre>
          <p className="mt-2 text-xs text-muted-foreground">
            {t(
              "Enrolling the engine in the CrowdSec console and sharing signals is done on the engine, not here: CrowdSec offers no way to do it remotely, and the controller cannot see the engine's enrollment. Read the runbook and CrowdSec's terms first; sharing is off in the reference setup.",
            )}
          </p>
        </details>
        <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 border-t pt-3">
          <dt className="text-muted-foreground">{t("Last sync")}</dt>
          <dd>{c.synced_at ? shortWhen(c.synced_at) : t("never")}</dd>
          <dt className="text-muted-foreground">{t("Networks banned")}</dt>
          <dd>{c.bans ?? "–"}</dd>
          {c.error && (
            <>
              <dt className="text-muted-foreground">{t("Last error")}</dt>
              <dd className="text-destructive">{c.error}</dd>
            </>
          )}
        </dl>
      </div>
    </section>
  );
}

export function Integrations() {
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">{t("Integrations")}</h1>
        <p className="text-sm text-muted-foreground">
          {t("Outside systems the controller reads from. Changes are recorded in the audit log.")}
        </p>
      </div>
      <CrowdSecCard />
    </div>
  );
}
