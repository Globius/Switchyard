import { Link, Outlet } from "@tanstack/react-router";
import { Badge } from "@/components/ui/badge";
import { DraftMarker, HealthPill } from "@/components/sy/StatusPill";
import { ErrorState } from "@/components/sy/states";
import { Skeleton } from "@/components/ui/skeleton";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { useVsStatus } from "./queries";
import { useVs } from "./detail-queries";

const TABS = [
  { label: "Overview", to: "/t/$tenant/virtual-services/$id" },
  { label: "Settings", to: "/t/$tenant/virtual-services/$id/settings" },
  { label: "Rendered config", to: "/t/$tenant/virtual-services/$id/rendered" },
  { label: "History", to: "/t/$tenant/virtual-services/$id/history" },
] as const;

/** Header and tabs shared by Overview, Settings, Rendered config and History. */
export function VsDetailLayout({ tenant, id }: { tenant: string; id: string }) {
  const { draft } = useDraft(tenant);
  const vs = useVs(tenant, id, draft?.id);
  const status = useVsStatus(tenant);
  if (vs.isPending) return <Skeleton className="h-16 w-full max-w-xl" />;
  if (vs.isError) return <ErrorState error={vs.error} onRetry={() => void vs.refetch()} />;
  const v = vs.data;
  const st = status.data?.find((s) => s.id === id);
  const health = st?.health;
  return (
    <div className="space-y-4">
      <nav aria-label="Breadcrumb" className="text-sm text-muted-foreground">
        <Link to="/t/$tenant/virtual-services" params={{ tenant }} className="hover:underline">
          {t("Virtual services")}
        </Link>{" "}
        / <span className="text-foreground">{v.name}</span>
      </nav>
      <div className="flex flex-wrap items-center gap-3">
        <h1
          className={
            v.draft_status === "deleted"
              ? "text-2xl font-semibold line-through"
              : "text-2xl font-semibold"
          }
        >
          {v.name}
        </h1>
        {v.draft_status !== "unchanged" && <DraftMarker status={v.draft_status} />}
        {v.enabled ? <HealthPill health={health} /> : <HealthPill health="disabled" />}
        {st && v.enabled && (
          <span className="text-sm text-muted-foreground">
            {t("{up} of {total} members up", { up: st.members_up, total: st.members_total })}
          </span>
        )}
        <Badge>{v.protocol.toUpperCase()}</Badge>
        <span className="font-mono text-sm text-muted-foreground">
          {v.vip ?? t("VIP allocated on apply")} · {v.ports.map((p) => p.port).join(", ")}
        </span>
      </div>
      <div role="tablist" className="flex gap-1 border-b">
        {TABS.map((tab) => (
          <Link
            key={tab.label}
            to={tab.to}
            params={{ tenant, id }}
            activeOptions={{ exact: true }}
            role="tab"
            className="-mb-px border-b-2 border-transparent px-4 py-2 text-sm text-muted-foreground hover:text-foreground"
            activeProps={{ className: "border-primary text-foreground font-medium" }}
          >
            {t(tab.label)}
          </Link>
        ))}
      </div>
      <Outlet />
    </div>
  );
}
