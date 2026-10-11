import { useNavigate, Link, Outlet } from "@tanstack/react-router";
import { useState } from "react";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { HealthPill, StatusPill } from "@/components/sy/StatusPill";
import { ErrorState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tip } from "@/components/ui/tooltip";
import { useSaveOp } from "@/features/objects/save";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { useVs } from "./detail-queries";
import { useSeGroups, useVsStatus } from "./queries";

const TABS = [
  { label: "Overview", to: "/t/$tenant/virtual-services/$id" },
  { label: "Settings", to: "/t/$tenant/virtual-services/$id/settings" },
  { label: "Rendered config", to: "/t/$tenant/virtual-services/$id/rendered" },
  { label: "History", to: "/t/$tenant/virtual-services/$id/history" },
] as const;

/** Header, actions and tabs shared by Overview, Settings, Rendered config and History. */
export function VsDetailLayout({ tenant, id }: { tenant: string; id: string }) {
  const { draft } = useDraft(tenant);
  const vs = useVs(tenant, id, draft?.id);
  const status = useVsStatus(tenant);
  const groups = useSeGroups(tenant);
  const can = useCan(tenant);
  const write = can("write", "application");
  const save = useSaveOp(tenant, "virtual_service");
  const navigate = useNavigate();
  const [confirmDelete, setConfirmDelete] = useState(false);
  if (vs.isPending) return <Skeleton className="h-16 w-full max-w-xl" />;
  if (vs.isError) return <ErrorState error={vs.error} onRetry={() => void vs.refetch()} />;
  const v = vs.data;
  const st = status.data?.find((s) => s.id === id);
  const groupName = groups.data?.items.find((g) => g.id === v.se_group_id)?.name;
  const deleted = v.draft_status === "deleted";
  const changed = v.draft_status !== "unchanged";
  return (
    <div className="space-y-4">
      <nav aria-label="Breadcrumb" className="text-sm text-muted-foreground">
        <Link
          to="/t/$tenant/virtual-services"
          params={{ tenant }}
          className="text-primary hover:underline"
        >
          {t("Virtual services")}
        </Link>{" "}
        / <span className="text-foreground">{v.name}</span>
      </nav>
      <div className="flex flex-wrap items-start gap-3">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-3">
            {changed && <span className="size-2 rounded-full bg-draft" aria-hidden />}
            <h1
              className={deleted ? "text-2xl font-semibold line-through" : "text-2xl font-semibold"}
            >
              {v.name}
            </h1>
            {v.enabled ? (
              <HealthPill
                health={st?.health}
                detail={
                  st
                    ? t("{up} of {total} members up", {
                        up: st.members_up,
                        total: st.members_total,
                      })
                    : undefined
                }
              />
            ) : (
              <HealthPill health="disabled" />
            )}
            {changed && (
              <StatusPill status="draft">
                {v.draft_status === "modified"
                  ? t("Modified in draft · {n} fields", { n: v.changed_fields.length })
                  : v.draft_status === "created"
                    ? t("New in draft")
                    : t("Deleted in draft")}
              </StatusPill>
            )}
          </div>
          <p className="font-mono text-sm text-muted-foreground">
            {v.vip ?? t("VIP allocated on apply")} · {v.ports.map((p) => p.port).join(", ")} ·{" "}
            {v.protocol.toUpperCase()} · {groupName ?? "—"}
          </p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Tip label={write.reason}>
            <Button
              variant="outline"
              disabled={!write.allowed || deleted}
              onClick={() =>
                void save.mutateAsync({
                  action: "update",
                  id: v.id,
                  version: v.version,
                  body: { enabled: !v.enabled },
                })
              }
            >
              {v.enabled ? t("Disable") : t("Enable")}
            </Button>
          </Tip>
          <Button variant="outline" asChild disabled={!write.allowed}>
            <Link
              to="/t/$tenant/virtual-services/$id/settings"
              params={{ tenant, id }}
              search={{ field: "routes" }}
            >
              {t("Add route")}
            </Link>
          </Button>
          <Tip label={write.reason}>
            <Button
              variant="ghost"
              disabled={!write.allowed || deleted}
              onClick={() => setConfirmDelete(true)}
            >
              {t("Delete")}
            </Button>
          </Tip>
        </div>
      </div>
      <div role="tablist" className="flex gap-1 border-b">
        {TABS.map((tab) => (
          <Link
            key={tab.label}
            to={tab.to}
            params={{ tenant, id }}
            search={tab.label === "Settings" ? { field: undefined } : undefined}
            activeOptions={{ exact: true }}
            role="tab"
            className="-mb-px flex items-center gap-2 border-b-2 border-transparent px-4 py-2 text-sm text-primary hover:text-foreground"
            activeProps={{ className: "border-primary text-foreground font-medium" }}
          >
            {t(tab.label)}
            {tab.label === "Settings" && changed && (
              <span className="size-2 rounded-full bg-draft" aria-label={t("Changed in draft")} />
            )}
          </Link>
        ))}
      </div>
      <Outlet />
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete {name}?", { name: v.name })}
        confirmLabel={t("Add delete to draft")}
        busy={save.isPending}
        onConfirm={async () => {
          try {
            await save.mutateAsync({ action: "delete", id: v.id, version: v.version });
            void navigate({ to: "/t/$tenant/virtual-services", params: { tenant } });
          } finally {
            setConfirmDelete(false);
          }
        }}
      >
        <p>
          {t(
            "The service is removed when the change set is applied, and its VIP goes back to its pool. Nothing changes until then.",
          )}
        </p>
      </ConfirmDialog>
    </div>
  );
}
