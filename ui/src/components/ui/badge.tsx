// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

/** Neutral pill; tinted variants are the status tokens (always pair with an icon and a label). */
const tones = {
  neutral: "bg-secondary text-secondary-foreground",
  draft: "bg-draft-bg text-draft",
  live: "bg-live-bg text-live",
  healthy: "bg-status-healthy-bg text-status-healthy",
  degraded: "bg-status-degraded-bg text-status-degraded",
  down: "bg-status-down-bg text-status-down",
  draining: "bg-status-draining-bg text-status-draining",
  pending: "bg-pending-approval-bg text-pending-approval",
} as const;

export function Badge({
  tone = "neutral",
  className,
  ...p
}: ComponentProps<"span"> & { tone?: keyof typeof tones }) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium",
        tones[tone],
        className,
      )}
      {...p}
    />
  );
}
