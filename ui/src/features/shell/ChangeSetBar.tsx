import { useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Clock, Pencil } from "lucide-react";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { ErrorState } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useCan } from "@/auth/session";
import { t } from "@/lib/i18n";
import type { ChangeSetSummary } from "./draft";

/** Fixed bottom bar shown whenever the caller has an open draft in the tenant. */
export function ChangeSetBar({ tenant, draft }: { tenant: string; draft: ChangeSetSummary }) {
  const can = useCan(tenant);
  const discard = can("edit", "draft");
  const pending = draft.state === "pending_approval";
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);

  async function doDiscard() {
    setBusy(true);
    try {
      unwrap(
        await api.POST("/api/v1/tenants/{t}/change-sets/{cs}/discard", {
          params: { path: { t: tenant, cs: draft.id } },
        }),
      );
      setProblem(null);
      await qc.invalidateQueries({ queryKey: ["t", tenant] });
    } catch (e) {
      setProblem(e);
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  }

  return (
    <>
      {problem != null && (
        <div className="fixed inset-x-0 bottom-16 z-30 mx-5">
          <ErrorState error={problem} />
        </div>
      )}
      <div
        role="region"
        aria-label={t("Change set")}
        className="fixed inset-x-0 bottom-0 z-30 flex items-center gap-4 border-t bg-card px-5 py-3"
      >
        {pending ? (
          <Badge tone="pending">
            <Clock className="size-3" /> {t("Awaiting approval")}
          </Badge>
        ) : (
          <Badge tone="draft">
            <Pencil className="size-3" /> {t("Draft")}
          </Badge>
        )}
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold">{draft.summary}</div>
          <div className="text-xs text-muted-foreground">
            {draft.author} · {t("base revision #{n}", { n: draft.base_revision })}
          </div>
        </div>
        <div className="text-sm text-muted-foreground">
          {t("{n} changes", { n: draft.op_count })}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Tip label={discard.reason}>
            <Button variant="ghost" disabled={!discard.allowed} onClick={() => setConfirm(true)}>
              {t("Discard")}
            </Button>
          </Tip>
          <Button asChild>
            <Link to="/t/$tenant/review/$cs" params={{ tenant, cs: draft.id }}>
              {t("Review & apply")}
            </Link>
          </Button>
        </div>
      </div>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={t("Discard this change set?")}
        confirmLabel={t("Discard")}
        busy={busy}
        onConfirm={() => void doDiscard()}
      >
        <p>
          {t(
            "{n} changes in '{name}' are deleted. Nothing has gone live, so nothing else changes.",
            { n: draft.op_count, name: draft.summary },
          )}
        </p>
      </ConfirmDialog>
    </>
  );
}
