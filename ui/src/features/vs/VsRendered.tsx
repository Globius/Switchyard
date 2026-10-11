import { useState } from "react";
import { InvalidDraftCard } from "@/components/sy/Findings";
import { RenderedConfigView } from "@/components/sy/RenderedConfigView";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { useVsRendered } from "./detail-queries";
import { findingsOfError } from "./findings";

/** What this service renders to; reads the draft version while a draft is open. */
export function VsRendered({ tenant, id }: { tenant: string; id: string }) {
  const { draft } = useDraft(tenant);
  const [live, setLive] = useState(false);
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
  return (
    <div className="space-y-4">
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
