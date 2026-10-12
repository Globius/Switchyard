// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { t } from "@/lib/i18n";

/**
 * Display name for a principal string such as `user:authentik:dana`, `token:ci-deploy` or
 * `user:local:breakglass`. Raw principals never reach the screen (artboards show names).
 *
 * @example
 * principalName("user:authentik:dana") // "dana"
 * principalName("user:local:breakglass") // "Break-glass"
 */
export function principalName(p: string | null | undefined): string {
  if (!p) return "—";
  const parts = p.split(":");
  const last = parts[parts.length - 1] ?? p;
  if (last === "breakglass") return t("Break-glass");
  if (parts[0] === "system") return t("System");
  return last || p;
}

/** True when the principal is the break-glass administrator. */
export function isBreakGlass(p: string | null | undefined): boolean {
  return !!p && p.endsWith(":breakglass");
}

const hhmm = (d: Date) =>
  d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

/**
 * Short timestamp used in lists: `today 08:12`, `yesterday 22:40`, otherwise `Oct 6 14:02`.
 *
 * @example
 * shortWhen("2026-10-10T08:12:00Z", new Date("2026-10-10T12:00:00Z")) // "today 08:12" (local time)
 */
export function shortWhen(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return "—";
  const d = new Date(iso);
  const days = Math.round(
    (new Date(now).setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 86_400_000,
  );
  if (days === 0) return `${t("today")} ${hhmm(d)}`;
  if (days === 1) return `${t("yesterday")} ${hhmm(d)}`;
  return `${d.toLocaleDateString("en-US", { month: "short", day: "numeric" })} ${hhmm(d)}`;
}

/** Calendar date without time: `Oct 22, 2026`. */
export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

/** Whole days from `now` until `iso` (negative when past). */
export function daysUntil(iso: string, now = new Date()): number {
  return Math.floor((new Date(iso).getTime() - now.getTime()) / 86_400_000);
}

/** Request ids are long; list rows show `req_8d21…a0f`. */
export function shortRequestId(id: string | null | undefined): string {
  if (!id) return "—";
  const bare = id.replace(/^req_/, "");
  return bare.length <= 8 ? `req_${bare}` : `req_${bare.slice(0, 4)}…${bare.slice(-3)}`;
}

const LABELS: Record<string, string> = {
  change_set: "Change set",
  virtual_service: "Virtual service",
  pool: "Pool",
  health_monitor: "Health monitor",
  certificate: "Certificate",
  policy_set: "Policy",
  routing_profile: "Routing profile",
  se_group: "SE group",
  engine: "Engine",
  network: "Network",
  vip_pool: "VIP pool",
  revision: "Revision",
  tenant: "Tenant",
  user: "User",
  token: "Token",
  job: "Job",
};

/**
 * Readable object reference for the audit log: `change_set:01a1285d-…` becomes `Change set 01a1285d`.
 * Names, when the event carries them, win over ids.
 */
export function objectLabel(ref: string | null | undefined): string {
  if (!ref) return "—";
  const i = ref.indexOf(":");
  if (i < 0) return ref;
  const kind = ref.slice(0, i);
  const rest = ref.slice(i + 1);
  const label = LABELS[kind];
  if (!label) return ref;
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(rest);
  return `${t(label)} ${uuid ? rest.slice(0, 8) : rest}`;
}
