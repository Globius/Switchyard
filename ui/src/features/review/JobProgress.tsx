// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { Check, Circle, Loader2, Minus, X } from "lucide-react";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import type { Job } from "./model";

const ICON = {
  succeeded: <Check className="size-4 text-status-healthy" aria-hidden />,
  failed: <X className="size-4 text-status-down" aria-hidden />,
  running: <Loader2 className="size-4 animate-spin text-pending-approval" aria-hidden />,
  skipped: <Minus className="size-4 text-muted-foreground" aria-hidden />,
  pending: <Circle className="size-4 text-muted-foreground" aria-hidden />,
} as const;

const LABEL = {
  succeeded: "done",
  failed: "failed",
  running: "running",
  skipped: "skipped",
  pending: "waiting",
} as const;

/** Seconds between two ISO times, or null. */
export function seconds(from?: string | null, to?: string | null): number | null {
  if (!from || !to) return null;
  return Math.max(0, (new Date(to).getTime() - new Date(from).getTime()) / 1000);
}

/** The apply stages (lock, render, validate, canary, rollout, commit) with state and duration. */
export function JobProgress({ stages = [] }: { stages?: Job["stages"] }) {
  return (
    <ol aria-label={t("Apply progress")} className="space-y-2 text-sm">
      {stages.map((s) => {
        const d = seconds(s.started_at, s.finished_at);
        return (
          <li key={s.name} className="space-y-0.5">
            <div
              className={cn(
                "flex items-center gap-2",
                s.state === "pending" && "text-muted-foreground",
              )}
            >
              {ICON[s.state]}
              <span className="capitalize">{s.name.replace(/_/g, " ")}</span>
              <span className="sr-only">{t(LABEL[s.state])}</span>
              {d !== null && (
                <span className="ml-auto text-xs text-muted-foreground">{d.toFixed(1)} s</span>
              )}
            </div>
            {s.error && <p className="pl-6 text-xs text-status-down">{s.error}</p>}
          </li>
        );
      })}
    </ol>
  );
}
