// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Names and thresholds for the quota views (tenant portal and the admin queue; ADR 0199). */
import type { components } from "@/api/schema";

export type QuotaRequest = components["schemas"]["QuotaRequestRead"];

/** What a quota key is called on screen. Keys are the API's object type names. */
export const RESOURCE_LABEL: Record<string, string> = {
  virtual_service: "Virtual services",
  pool: "Pools",
  health_monitor: "Health monitors",
  policy_set: "Policy sets",
  certificate: "Certificates",
  network: "Networks",
  vip_pool: "VIP pools",
  se_group: "SE groups",
  routing_profile: "Routing profiles",
  stick_table_entries: "Stick table entries",
};

export function resourceLabel(key: string): string {
  return RESOURCE_LABEL[key] ?? key.replace(/_/g, " ");
}

export type UsageTone = "healthy" | "degraded" | "down";

/**
 * How full a quota is: `healthy` below 80 %, `degraded` from 80 % and `down` when the limit is
 * reached (nothing more can be added). A limit of 0 is always full.
 */
export function usageTone(used: number, limit: number): UsageTone {
  if (limit <= 0 || used >= limit) return "down";
  return used / limit >= 0.8 ? "degraded" : "healthy";
}

/** `used` as a share of `limit`, 0 to 100, for the bar. */
export function usagePercent(used: number, limit: number): number {
  if (limit <= 0) return 100;
  return Math.min(100, Math.round((used / limit) * 100));
}

/** A starting point for "how many do you need": the limit plus half again, at least one more. */
export function suggestedLimit(limit: number): number {
  return limit + Math.max(1, Math.ceil(limit / 2));
}
