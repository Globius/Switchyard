/**
 * Field-level findings from the API (`errors[]` of a 422, or the validator's `problems`):
 * a summary that links each one to its field, and the "your draft is invalid" card.
 */
import { Link } from "@tanstack/react-router";
import { AlertTriangle } from "lucide-react";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";

export interface Finding {
  path: string;
  message: string;
  code?: string;
}

/** Last segment of a dotted or bracketed path, used as the Settings field to open. */
export function fieldOf(path: string): string {
  const parts = path
    .replace(/\[(\d+)\]/g, ".$1")
    .split(".")
    .filter(Boolean);
  const i = parts.findIndex((p) =>
    ["ports", "routes", "certificate_ids", "policy_set_ids"].includes(p),
  );
  if (i >= 0) return parts.slice(i, i + 2).join(".");
  return parts.at(-1) ?? path;
}

/** Move focus to the input that carries `path` (its `data-field`), if the form has one. */
export function focusField(path: string, root: ParentNode = document): boolean {
  const field = fieldOf(path);
  const candidates = [field, field.split(".")[0] ?? field];
  for (const c of candidates) {
    const el = root.querySelector<HTMLElement>(`[data-field="${CSS.escape(c)}"]`);
    if (!el) continue;
    const input = el.matches("input,select,textarea,button")
      ? el
      : (el.querySelector<HTMLElement>("[data-focus]") ??
        el.querySelector<HTMLElement>("input,select,textarea,button"));
    (input ?? el).focus();
    el.scrollIntoView?.({ block: "center" });
    return true;
  }
  return false;
}

/** True when the form has an input for this finding's path. */
export function canPlace(path: string, root: ParentNode = document): boolean {
  const field = fieldOf(path);
  return [field, field.split(".")[0] ?? field].some((c) =>
    root.querySelector(`[data-field="${CSS.escape(c)}"]`),
  );
}

/**
 * Summary alert for a form with several findings: each links to its field and paths the form
 * cannot place stay here only. Inline errors on the inputs remain.
 */
export function FindingsSummary({ findings }: { findings: Finding[] }) {
  if (findings.length === 0) return null;
  return (
    <div
      role="alert"
      className="space-y-2 rounded-lg border border-status-down/30 bg-status-down-bg p-4 text-sm"
    >
      <div className="flex items-center gap-2 font-semibold text-status-down">
        <AlertTriangle className="size-4" aria-hidden />
        {t("{n} things need fixing", { n: findings.length })}
      </div>
      <ul className="space-y-1">
        {findings.map((f, i) => (
          <li key={`${f.path}-${i}`}>
            {canPlace(f.path) ? (
              <button
                type="button"
                className="text-left text-primary hover:underline"
                onClick={() => focusField(f.path)}
              >
                {f.message}
              </button>
            ) : (
              <span>{f.message}</span>
            )}{" "}
            <span className="font-mono text-xs text-muted-foreground">{f.path}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Replaces a tab's content when the draft cannot be rendered or fails the validator. */
export function InvalidDraftCard({
  tenant,
  serviceId,
  findings,
  onShowLive,
  extra,
}: {
  tenant: string;
  serviceId: string;
  findings: Finding[];
  onShowLive?: () => void;
  extra?: ReactNode;
}) {
  return (
    <section
      role="alert"
      className="space-y-4 rounded-lg border border-status-down/30 bg-status-down-bg p-5"
    >
      <div>
        <h2 className="text-base font-semibold text-status-down">{t("Your draft is invalid")}</h2>
        <p className="text-sm text-foreground">
          {t(
            "This service cannot be shown with your draft applied. Fix the findings below, or look at the live version.",
          )}
        </p>
      </div>
      <ul className="divide-y rounded-md border bg-card">
        {findings.map((f, i) => (
          <li key={`${f.path}-${i}`} className="flex items-center gap-3 px-3 py-2 text-sm">
            <span className="flex-1">
              {f.message}
              <span className="ml-2 font-mono text-xs text-muted-foreground">{f.path}</span>
            </span>
            <Link
              to="/t/$tenant/virtual-services/$id/settings"
              params={{ tenant, id: serviceId }}
              search={{ field: fieldOf(f.path) }}
              className="text-primary hover:underline"
            >
              {t("Go to field")}
            </Link>
          </li>
        ))}
      </ul>
      {extra}
      {onShowLive && (
        <Button type="button" variant="outline" onClick={onShowLive}>
          {t("Show the live version")}
        </Button>
      )}
    </section>
  );
}
