/**
 * "Re-apply": pushes the desired state to a drifted SE group again. It runs as a job
 * (`reapply_se_group`), creates no revision and no draft, and needs tenant-admin.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { ErrorState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { t } from "@/lib/i18n";

export function ReapplyButton({
  tenant,
  groupId,
  engine,
}: {
  tenant: string;
  groupId: string;
  engine?: string;
}) {
  const can = useCan(tenant);
  const allowed = can("manage", "engine");
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState(false);
  const go = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/se-groups/{group}/reapply", {
          params: { path: { t: tenant, group: groupId } },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["t", tenant] }),
  });
  return (
    <>
      <Tip label={allowed.reason}>
        <Button
          size="sm"
          variant="outline"
          disabled={!allowed.allowed || go.isPending}
          onClick={() => setConfirm(true)}
        >
          {t("Re-apply desired state")}
        </Button>
      </Tip>
      {go.isSuccess && (
        <p role="status" className="text-xs">
          {t("Re-apply started.")}{" "}
          <Link to="/t/$tenant/jobs" params={{ tenant }} className="text-primary hover:underline">
            {t("Open job")}
          </Link>
        </p>
      )}
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={t("Re-apply the desired state?")}
        confirmLabel={t("Re-apply")}
        busy={go.isPending}
        onConfirm={() => {
          setConfirm(false);
          go.mutate();
        }}
      >
        <p>
          {engine
            ? t(
                "Pushes the current desired state to {engine} and the rest of its SE group again.",
                {
                  engine,
                },
              )
            : t("Pushes the current desired state to every engine of the SE group again.")}{" "}
          {t(
            "It runs as a job and creates no revision and no draft. Drift is only reported until you do this.",
          )}
        </p>
      </ConfirmDialog>
      {go.error && <ErrorState error={go.error} />}
    </>
  );
}
