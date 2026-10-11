import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import { InvalidDraftCard } from "@/components/sy/Findings";
import { RenderedConfigView } from "@/components/sy/RenderedConfigView";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { useVsRendered } from "./detail-queries";
import { findingsOfError } from "./findings";

/** The files one engine gets for this service (group files plus its own), from the API. */
function EngineRendered({ tenant, id, engine }: { tenant: string; id: string; engine: string }) {
  const q = useQuery({
    queryKey: ["t", tenant, "virtual-services", id, "engines", engine, "rendered"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/virtual-services/{service}/engines/{engine}/rendered", {
          params: { path: { t: tenant, service: id, engine } },
        }),
      ),
  });
  if (q.isPending) return <TableSkeleton rows={4} cols={1} />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">
        {t("What {engine} runs for this service. Secrets are redacted.", { engine: q.data.engine })}
      </p>
      {q.data.files.map((f) => (
        <RenderedConfigView
          key={`${f.scope}-${f.file}`}
          fragment={{
            file: `${f.file} · ${f.scope === "engine" ? t("this engine") : t("SE group")}`,
            content: f.content,
            language: f.language,
            se_group_id: q.data.se_group_id,
            fingerprint: q.data.fingerprint,
          }}
        />
      ))}
    </div>
  );
}

/** What this service renders to; reads the draft version while a draft is open. */
export function VsRendered({ tenant, id }: { tenant: string; id: string }) {
  const { draft } = useDraft(tenant);
  const [live, setLive] = useState(false);
  const [engine, setEngine] = useState("");
  const engines = useQuery({
    queryKey: ["t", tenant, "virtual-services", id, "engines"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/virtual-services/{service}/engines", {
          params: { path: { t: tenant, service: id } },
        }),
      ),
  });
  const q = useVsRendered(tenant, id, live ? undefined : draft?.id);
  if (q.isPending) return <TableSkeleton rows={5} cols={1} />;
  if (q.isError) {
    const findings = findingsOfError(q.error);
    if (findings.length > 0 && !live)
      return (
        <InvalidDraftCard
          tenant={tenant}
          serviceId={id}
          findings={findings}
          onShowLive={() => setLive(true)}
        />
      );
    return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  }
  if (q.data.items.length === 0)
    return (
      <EmptyState
        title={t("Nothing is rendered")}
        body={t(
          "This service is disabled, or it is UDP, or no pool serves it, so no HAProxy config is produced.",
        )}
      />
    );
  if (engine)
    return (
      <div className="space-y-4">
        {(engines.data?.length ?? 0) > 0 && (
          <label className="flex items-center gap-2 text-sm">
            <span className="font-medium">{t("Show config for")}</span>
            <select
              aria-label={t("Engine")}
              className="h-9 rounded-md border border-input bg-card px-3 text-sm"
              value={engine}
              onChange={(e) => setEngine(e.target.value)}
            >
              <option value="">{t("The SE group (shared files)")}</option>
              {engines.data?.map((e) => (
                <option key={e.id} value={e.id}>
                  {e.name}
                  {e.mgmt_ip ? ` · ${e.mgmt_ip}` : ""}
                </option>
              ))}
            </select>
          </label>
        )}
        <EngineRendered tenant={tenant} id={id} engine={engine} />
      </div>
    );
  return (
    <div className="space-y-4">
      {(engines.data?.length ?? 0) > 0 && (
        <label className="flex items-center gap-2 text-sm">
          <span className="font-medium">{t("Show config for")}</span>
          <select
            aria-label={t("Engine")}
            className="h-9 rounded-md border border-input bg-card px-3 text-sm"
            value={engine}
            onChange={(e) => setEngine(e.target.value)}
          >
            <option value="">{t("The SE group (shared files)")}</option>
            {engines.data?.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
                {e.mgmt_ip ? ` · ${e.mgmt_ip}` : ""}
              </option>
            ))}
          </select>
        </label>
      )}
      {draft && (
        <div
          role="radiogroup"
          aria-label={t("Version")}
          className="inline-flex overflow-hidden rounded-md border"
        >
          {([false, true] as const).map((isLive) => (
            <button
              key={String(isLive)}
              type="button"
              role="radio"
              aria-checked={live === isLive}
              onClick={() => setLive(isLive)}
              className={cn(
                "px-3 py-1.5 text-sm",
                live === isLive
                  ? "bg-accent font-medium"
                  : "text-muted-foreground hover:bg-accent/50",
              )}
            >
              {isLive ? t("Live") : t("With my draft")}
            </button>
          ))}
        </div>
      )}
      {draft && (
        <p className="text-sm text-muted-foreground">
          {live
            ? t("Showing the live version, without your draft.")
            : t("Showing the config with your draft applied.")}
        </p>
      )}
      {q.data.items.map((f, i) => (
        <RenderedConfigView key={`${f.file}-${i}`} fragment={f} />
      ))}
    </div>
  );
}
