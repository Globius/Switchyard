import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { t } from "@/lib/i18n";
import { useObjectAudit, useObjectRevisions, useVs } from "./detail-queries";
import { principalName, shortWhen } from "@/lib/format";

/** Revisions and audit events for one service. */
export function VsHistory({ tenant, id }: { tenant: string; id: string }) {
  const vs = useVs(tenant, id);
  const ref = `virtual_service:${id}`;
  const revisions = useObjectRevisions(tenant, id);
  const audit = useObjectAudit(tenant, ref);
  if (revisions.isPending || audit.isPending || vs.isPending)
    return <TableSkeleton rows={5} cols={3} />;
  const err = revisions.error ?? audit.error;
  if (err)
    return (
      <ErrorState
        error={err}
        onRetry={() => {
          void revisions.refetch();
          void audit.refetch();
        }}
      />
    );
  const empty = revisions.data!.items.length === 0 && audit.data!.items.length === 0;
  if (empty)
    return (
      <EmptyState
        title={t("No history yet")}
        body={t("Changes to this service appear here once a revision applies.")}
      />
    );
  return (
    <div className="space-y-6">
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">{t("Revisions")}</h2>
        <ul className="divide-y rounded-lg border bg-card">
          {revisions.data!.items.map((r) => (
            <li key={r.number} className="flex items-center gap-4 px-4 py-3 text-[13px]">
              <span className="font-mono">#{r.number}</span>
              <span className="flex-1">{r.summary}</span>
              <span className="text-muted-foreground">{principalName(r.author)}</span>
              <time className="text-muted-foreground" dateTime={r.created_at}>
                {shortWhen(r.created_at)}
              </time>
            </li>
          ))}
        </ul>
      </section>
      <section className="space-y-2">
        <h2 className="text-lg font-semibold">{t("Audit events")}</h2>
        <ul className="divide-y rounded-lg border bg-card">
          {audit.data!.items.map((e, i) => (
            <li key={`${e.at}-${i}`} className="flex items-center gap-4 px-4 py-3 text-[13px]">
              <span className="font-mono">{e.action}</span>
              <span className="text-muted-foreground">{principalName(e.actor)}</span>
              <time className="ml-auto text-muted-foreground" dateTime={e.at}>
                {shortWhen(e.at)}
              </time>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
