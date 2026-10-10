import {
  AlertTriangle,
  ArrowDownToLine,
  CheckCircle2,
  Clock,
  Pencil,
  XCircle,
  Zap,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { t } from "@/lib/i18n";

type Status =
  "healthy" | "degraded" | "down" | "draining" | "draft" | "live" | "pending" | "neutral";

const ICON = {
  healthy: CheckCircle2,
  degraded: AlertTriangle,
  down: XCircle,
  draining: ArrowDownToLine,
  draft: Pencil,
  live: Zap,
  pending: Clock,
} as const;

/** Status as icon plus label, never colour alone (handoff status vocabulary). */
export function StatusPill({ status, children }: { status: Status; children: string }) {
  const Icon = status === "neutral" ? null : ICON[status];
  return (
    <Badge tone={status}>
      {Icon && <Icon className="size-3" aria-hidden />}
      {children}
    </Badge>
  );
}

/** Health as the API reports it for a virtual service. */
export function HealthPill({ health }: { health: string | undefined }) {
  switch (health) {
    case "healthy":
      return <StatusPill status="healthy">{t("Healthy")}</StatusPill>;
    case "degraded":
      return <StatusPill status="degraded">{t("Degraded")}</StatusPill>;
    case "down":
      return <StatusPill status="down">{t("Down")}</StatusPill>;
    case "disabled":
      return <StatusPill status="neutral">{t("Disabled")}</StatusPill>;
    default:
      return <StatusPill status="neutral">{t("Unknown")}</StatusPill>;
  }
}

/** Marker for objects that differ from live because of the open draft. */
export function DraftMarker({ status }: { status: "created" | "modified" | "deleted" }) {
  const label = { created: t("New"), modified: t("Modified"), deleted: t("Deleted") }[status];
  return (
    <>
      <span className="size-2 rounded-full bg-draft" aria-hidden />
      <StatusPill status="draft">{label}</StatusPill>
    </>
  );
}

/** Certificate expiry: 30 days or less warns, 7 or less is danger, past is expired (icon + text). */
export function expiryStatus(notAfter: string, now = Date.now()) {
  const days = Math.floor((new Date(notAfter).getTime() - now) / 86_400_000);
  if (days < 0) return { days, status: "down" as const, label: t("Expired") };
  if (days <= 7) return { days, status: "down" as const, label: t("{n} days", { n: days }) };
  if (days <= 30) return { days, status: "degraded" as const, label: t("{n} days", { n: days }) };
  return { days, status: "neutral" as const, label: t("{n} days", { n: days }) };
}
