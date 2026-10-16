// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Admin: controller-wide settings (system-admin role). Today: whether SE groups may be containers. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { ErrorState, TableSkeleton } from "@/components/sy/states";
import { Switch } from "@/components/ui/switch";
import { GitMirror } from "@/features/admin/GitMirror";
import { t } from "@/lib/i18n";

export function SystemSettings() {
  const qc = useQueryClient();
  const [confirmOn, setConfirmOn] = useState(false);
  const q = useQuery({
    queryKey: ["admin", "settings"],
    queryFn: async () => unwrap(await api.GET("/api/v1/system/settings")),
  });
  const save = useMutation({
    mutationFn: async (allow: boolean) =>
      unwrap(await api.PATCH("/api/v1/system/settings", { body: { allow_lxc_guests: allow } })),
    onSuccess: () => {
      setConfirmOn(false);
      // /me carries `features.lxc_guests` for the deploy form.
      void qc.invalidateQueries({ queryKey: ["admin", "settings"] });
      void qc.invalidateQueries({ queryKey: ["me"] });
    },
    onError: () => setConfirmOn(false),
  });
  const s = q.data;
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">{t("System settings")}</h1>
        <p className="text-sm text-muted-foreground">
          {t("Changes here are immediate and recorded in the audit log")}
        </p>
      </div>
      {q.isPending ? (
        <TableSkeleton rows={2} cols={2} />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : (
        <section className="overflow-hidden rounded-lg border bg-card">
          <header className="flex items-center gap-3 border-b px-4 py-3">
            <h2 className="text-base font-semibold">{t("Container (LXC) guests")}</h2>
            <StatusPill status={s!.allow_lxc_guests ? "degraded" : "neutral"}>
              {s!.allow_lxc_guests ? t("Allowed") : t("Off")}
            </StatusPill>
          </header>
          <div className="space-y-3 p-4">
            {save.error && <ErrorState error={save.error} />}
            <div className="flex items-start gap-3">
              <Switch
                aria-label={t("Allow container (LXC) guests")}
                checked={s!.allow_lxc_guests}
                disabled={s!.allow_lxc_guests_forced || save.isPending}
                onCheckedChange={(on) => (on ? setConfirmOn(true) : save.mutate(false))}
              />
              <div className="space-y-1 text-sm">
                <div className="font-medium">{t("Allow container (LXC) guests")}</div>
                <p className="text-muted-foreground">
                  {t(
                    "Lets tenant-admins deploy SE groups as containers. A container engine is privileged with nesting on, so root in it is root on the Proxmox host. Off by default; while it is off, deploying a container is refused.",
                  )}
                </p>
                <p className="text-muted-foreground">
                  {t(
                    "Turning it off does not stop containers that already run or deploy jobs already queued.",
                  )}
                </p>
              </div>
            </div>
            <p className="text-xs text-muted-foreground">
              {t("Who changed this and when is in the")}{" "}
              <Link to="/admin/audit-log" className="text-primary hover:underline">
                {t("audit log")}
              </Link>
              .
            </p>
            {s!.allow_lxc_guests_forced && (
              <p role="note" className="rounded-md bg-muted p-3 text-sm">
                {t(
                  "This deployment forces containers on (SWITCHYARD_ALLOW_LXC). The stored value has no effect while it is set, so it cannot be changed here.",
                )}
              </p>
            )}
          </div>
        </section>
      )}
      <GitMirror />
      <ConfirmDialog
        open={confirmOn}
        onOpenChange={setConfirmOn}
        title={t("Allow container guests?")}
        confirmLabel={t("Allow containers")}
        busy={save.isPending}
        onConfirm={() => save.mutate(true)}
      >
        <p>
          <StatusPill status="down">{t("Live")}</StatusPill>{" "}
          {t(
            "Tenant-admins will be able to deploy privileged containers on your Proxmox hosts. This takes effect immediately and is audited.",
          )}
        </p>
      </ConfirmDialog>
    </div>
  );
}
