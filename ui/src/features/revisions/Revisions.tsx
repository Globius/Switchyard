import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { collectionLabel, diffSnapshots, show, type Collections } from "@/lib/snapshot-diff";
import { cn } from "@/lib/utils";

function useRevisions(tenant: string) {
  return useInfiniteQuery({
    queryKey: ["t", tenant, "revisions"],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/revisions", {
          params: { path: { t: tenant }, query: { cursor: pageParam, limit: 50 } },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

function useRevision(tenant: string, n: number | null) {
  return useQuery({
    queryKey: ["t", tenant, "revisions", n],
    enabled: n !== null,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/revisions/{n}", {
          params: { path: { t: tenant, n: n! } },
        }),
      ),
  });
}

/** Compared in the browser from the two revisions' snapshots. */
function Compare({ tenant, from, to }: { tenant: string; from: number; to: number }) {
  const a = useRevision(tenant, from);
  const b = useRevision(tenant, to);
  if (a.isPending || b.isPending) return <TableSkeleton rows={3} cols={2} />;
  const err = a.error ?? b.error;
  if (err)
    return (
      <ErrorState
        error={err}
        onRetry={() => {
          void a.refetch();
          void b.refetch();
        }}
      />
    );
  const changes = diffSnapshots(a.data!.snapshot as Collections, b.data!.snapshot as Collections);
  if (changes.length === 0)
    return (
      <p className="text-sm text-muted-foreground">
        {t("No differences between #{a} and #{b}.", { a: from, b: to })}
      </p>
    );
  return (
    <div className="space-y-3">
      {changes.map((c) => (
        <div key={`${c.collection}-${c.id}`} className="rounded-lg border bg-card">
          <div className="flex items-center gap-2 border-b px-4 py-2 text-sm">
            <span className="text-muted-foreground">{collectionLabel(c.collection)}</span>
            <span className="font-medium">{c.name}</span>
            <StatusPill status={c.status === "deleted" ? "down" : "draft"}>
              {{ created: t("New"), modified: t("Modified"), deleted: t("Deleted") }[c.status]}
            </StatusPill>
          </div>
          <div className="divide-y font-mono text-xs">
            {c.fields.map((f) => (
              <div key={f.field} className="px-4 py-1.5">
                <div className="text-muted-foreground">{f.field}</div>
                {c.status !== "created" && (
                  <div className="bg-diff-del px-1">− {show(f.before)}</div>
                )}
                {c.status !== "deleted" && (
                  <div className="bg-diff-add px-1">+ {show(f.after)}</div>
                )}
              </div>
            ))}
          </div>
        </div>
      ))}
      <p className="text-xs text-muted-foreground">
        {t(
          "Compared in the browser from the two revisions' snapshots. Secrets are references, so none appear here.",
        )}
      </p>
    </div>
  );
}

/** Revision timeline: pick two to compare, or roll back to one. */
export function Revisions({ tenant }: { tenant: string }) {
  const q = useRevisions(tenant);
  const can = useCan(tenant);
  const rollback = can("rollback", "revision");
  const { draft } = useDraft(tenant);
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [picked, setPicked] = useState<number[]>([]);
  const [target, setTarget] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);

  const rows = q.data?.pages.flatMap((p) => p.items) ?? [];
  const toggle = (n: number) =>
    setPicked((p) => (p.includes(n) ? p.filter((x) => x !== n) : [...p, n].slice(-2)));
  const [lo, hi] = [...picked].sort((x, y) => x - y);

  async function doRollback() {
    if (target === null) return;
    setBusy(true);
    setProblem(null);
    try {
      const cs = unwrap(
        await api.POST("/api/v1/tenants/{t}/revisions/{n}/rollback", {
          params: { path: { t: tenant, n: target } },
        }),
      );
      await qc.invalidateQueries({ queryKey: ["t", tenant] });
      void navigate({ to: "/t/$tenant/review/$cs", params: { tenant, cs: cs.id } });
    } catch (e) {
      setProblem(e);
      setTarget(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">{t("Revisions")}</h1>
        <p className="text-sm text-muted-foreground">
          {t("Every apply is a revision. Pick two to compare, or roll back to one.")}
        </p>
      </div>
      {problem != null && <ErrorState error={problem} />}
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
        <div className="space-y-3">
          <div className="rounded-lg border bg-card">
            {q.isPending ? (
              <TableSkeleton cols={4} />
            ) : q.isError ? (
              <ErrorState error={q.error} onRetry={() => void q.refetch()} />
            ) : rows.length === 0 ? (
              <EmptyState
                title={t("No revisions yet")}
                body={t("The first applied change set creates revision #1.")}
              />
            ) : (
              <ul className="divide-y">
                {rows.map((r, i) => (
                  <li
                    key={r.number}
                    className={cn(
                      "flex items-start gap-3 px-4 py-3 text-[13px]",
                      picked.includes(r.number) && "bg-accent/40",
                    )}
                  >
                    <span
                      aria-hidden
                      className={cn(
                        "mt-1.5 size-2 shrink-0 rounded-full",
                        i === 0 ? "bg-primary" : "bg-muted-foreground/40",
                      )}
                    />
                    <div className="min-w-0 flex-1 space-y-1">
                      <div>
                        <span className="font-mono font-semibold">#{r.number}</span> {r.summary}
                        {i === 0 && (
                          <span className="ml-2">
                            <StatusPill status="healthy">{t("current")}</StatusPill>
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {r.author} ·{" "}
                        <time dateTime={r.created_at}>
                          {new Date(r.created_at).toLocaleString()}
                        </time>
                        {r.git_sha && (
                          <>
                            {" "}
                            · git <span className="font-mono">{r.git_sha.slice(0, 7)}</span>
                          </>
                        )}
                      </div>
                      {i > 0 && (
                        <Tip label={rollback.reason}>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={!rollback.allowed}
                            onClick={() => setTarget(r.number)}
                          >
                            {t("Roll back")}
                          </Button>
                        </Tip>
                      )}
                    </div>
                    <label className="flex shrink-0 items-center gap-1.5">
                      <input
                        type="checkbox"
                        aria-label={t("Compare #{n}", { n: r.number })}
                        checked={picked.includes(r.number)}
                        onChange={() => toggle(r.number)}
                      />
                      {t("Compare")}
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </div>
          {q.hasNextPage && (
            <Button
              variant="outline"
              disabled={q.isFetchingNextPage}
              onClick={() => void q.fetchNextPage()}
            >
              {t("Load more")}
            </Button>
          )}
        </div>
        {lo !== undefined && hi !== undefined ? (
          <section className="space-y-3 rounded-lg border bg-card p-4">
            <h2 className="text-lg font-semibold">
              #{lo} → #{hi}
            </h2>
            <Compare tenant={tenant} from={lo} to={hi} />
          </section>
        ) : (
          <p className="rounded-lg border bg-card p-4 text-sm text-muted-foreground">
            {picked.length === 1
              ? t("Pick one more revision to compare.")
              : t("Pick two revisions to compare them.")}
          </p>
        )}
      </div>
      <ConfirmDialog
        open={target !== null}
        onOpenChange={(o) => !o && setTarget(null)}
        title={t("Roll back to #{n}?", { n: target ?? 0 })}
        confirmLabel={t("Create rollback draft")}
        busy={busy}
        onConfirm={() => void doRollback()}
      >
        <p>
          {t(
            "This opens a new draft that undoes the revisions after #{n}, then takes you to the usual review. Nothing changes until you apply it, with the same canary and approval steps as any change.",
            { n: target ?? 0 },
          )}
        </p>
        {draft && (
          <p>
            {t(
              "The rollback is its own change set. Your open draft with {c} changes is not touched.",
              { c: draft.op_count },
            )}
          </p>
        )}
      </ConfirmDialog>
    </div>
  );
}
