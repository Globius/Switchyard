/** "Re-apply revision #N": opens a rollback draft to the live revision so a drifted engine can be put back through review. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { ErrorState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { t } from "@/lib/i18n";

/** The newest revision number, or undefined while loading or when there is none. */
export function useLiveRevision(tenant: string): number | undefined {
  const q = useQuery({
    queryKey: ["t", tenant, "revisions", { live: true }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/revisions", {
          params: { path: { t: tenant }, query: { limit: 1 } },
        }),
      ),
  });
  return q.data?.items[0]?.number;
}

export function ReapplyButton({ tenant, engine }: { tenant: string; engine?: string }) {
  const rev = useLiveRevision(tenant);
  const can = useCan(tenant);
  const allowed = can("rollback", "revision");
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const go = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/revisions/{n}/rollback", {
          params: { path: { t: tenant, n: rev! } },
        }),
      ),
    onSuccess: async (cs) => {
      await qc.invalidateQueries({ queryKey: ["t", tenant] });
      void navigate({ to: "/t/$tenant/review/$cs", params: { tenant, cs: cs.id } });
    },
  });
  if (rev === undefined) return null;
  return (
    <>
      <Tip label={allowed.reason}>
        <Button
          size="sm"
          variant="outline"
          disabled={!allowed.allowed || go.isPending}
          onClick={() => setConfirm(true)}
        >
          {t("Re-apply revision #{n}", { n: rev })}
        </Button>
      </Tip>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={t("Re-apply revision #{n}?", { n: rev })}
        confirmLabel={t("Open draft")}
        busy={go.isPending}
        onConfirm={() => {
          setConfirm(false);
          go.mutate();
        }}
      >
        <p>
          {engine
            ? t(
                "Opens a draft that puts {engine} and every other engine back to what revision #{n} rendered.",
                {
                  engine,
                  n: rev,
                },
              )
            : t("Opens a draft at revision #{n} so engines that drifted can be put back.", {
                n: rev,
              })}{" "}
          {t("Nothing changes until you apply it, with the usual canary and approval steps.")}
        </p>
      </ConfirmDialog>
      {go.error && <ErrorState error={go.error} />}
    </>
  );
}
