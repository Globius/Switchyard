/**
 * The standard states every screen shows (handoff `States` artboard): loading skeleton rows,
 * empty, error from a problem+json body, and "unavailable backend" with the API's reason.
 */
import { AlertTriangle, Info } from "lucide-react";
import type { ReactNode } from "react";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { t } from "@/lib/i18n";

/** Skeleton rows in the real layout; never a blank page or a full-page spinner. */
export function TableSkeleton({ rows = 6, cols = 6 }: { rows?: number; cols?: number }) {
  return (
    <div role="status" aria-label={t("Loading")} className="divide-y">
      {Array.from({ length: rows }, (_, r) => (
        <div key={r} className="flex items-center gap-6 px-4 py-3.5">
          {Array.from({ length: cols }, (_, c) => (
            <Skeleton key={c} className={c === 0 ? "h-3 w-32" : "h-3 flex-1"} />
          ))}
        </div>
      ))}
    </div>
  );
}

/** Empty list: short explanation plus the primary action (text only for read-only users). */
export function EmptyState({
  title,
  body,
  action,
}: {
  title: string;
  body: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-14 text-center">
      <h2 className="text-base font-semibold">{title}</h2>
      <p className="max-w-md text-sm text-muted-foreground">{body}</p>
      {action}
    </div>
  );
}

/** Problem title and detail with Retry; field errors belong on the input instead. */
export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const p = error instanceof ApiError ? error.problem : undefined;
  return (
    <div
      role="alert"
      className="m-4 flex items-start gap-3 rounded-lg border border-status-down/30 bg-status-down-bg p-4 text-status-down"
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0" />
      <div className="flex-1 text-sm">
        <div className="font-semibold">{p?.title ?? t("Something went wrong")}</div>
        <div className="text-foreground">
          {p?.detail ?? (error instanceof Error ? error.message : t("Unknown error."))}
        </div>
        {p?.errors && p.errors.length > 0 && (
          <ul className="mt-2 list-disc space-y-0.5 pl-5 text-foreground">
            {p.errors.map((e, i) => (
              <li key={`${e.path}-${i}`}>
                <span className="font-mono text-xs">{e.path}</span>: {e.message}
              </li>
            ))}
          </ul>
        )}
        {error instanceof ApiError && (
          <div className="mt-1 font-mono text-xs text-muted-foreground">{error.status}</div>
        )}
      </div>
      {onRetry && (
        <Button variant="outline" size="sm" onClick={onRetry}>
          {t("Retry")}
        </Button>
      )}
    </div>
  );
}

/** A part of the screen whose backend is not configured; shows the API's `reason`. */
export function UnavailableState({ title, reason }: { title: string; reason?: string | null }) {
  return (
    <div className="flex items-center justify-center gap-2 rounded-lg border border-dashed px-4 py-5 text-center text-xs text-muted-foreground">
      <Info className="size-4 shrink-0" aria-hidden />
      <span>
        <span className="font-medium">{title}</span>
        {reason ? `: ${reason}` : ""}
      </span>
    </div>
  );
}
