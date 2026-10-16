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

function CrowdSecCard() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["admin", "crowdsec"],
    queryFn: async () => unwrap(await api.GET("/api/v1/system/crowdsec")),
  });
  const [url, setUrl] = useState<string | null>(null);
  const [origins, setOrigins] = useState<string | null>(null);
  const [logs, setLogs] = useState<string | null>(null);
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
            key_ref: c.key_ref,
            ...(key ? { api_key: key } : {}),
          },
        }),
      ),
    onSuccess: () => {
      setUrl(null);
      setOrigins(null);
      setLogs(null);
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
  const dirty = url !== null || origins !== null || logs !== null || key !== "";
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
