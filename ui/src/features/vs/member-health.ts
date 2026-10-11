/** Per-member health from engine status: the worst state across engines, and how recent the check is. */
import type { components } from "@/api/schema";

export type MemberRow = components["schemas"]["VirtualServiceMemberRead"];
export type Health = "up" | "down" | "drain" | "maint" | "unknown";

const ORDER: Record<Health, number> = { down: 4, drain: 3, maint: 2, unknown: 1, up: 0 };

/** HAProxy server status (`UP`, `DOWN`, `DRAIN`, `MAINT`, `NOLB`, ...) as one of five states. */
export function healthOf(status: string): Health {
  const s = status.toUpperCase();
  if (s.startsWith("UP")) return "up";
  if (s.startsWith("DOWN") || s === "NOLB") return "down";
  if (s.startsWith("DRAIN")) return "drain";
  if (s.startsWith("MAINT")) return "maint";
  return "unknown";
}

/** The worst row wins (an engine that sees the member down matters more than one that sees it up). */
export function worstOf(rows: MemberRow[]): MemberRow | undefined {
  return [...rows].sort((a, b) => ORDER[healthOf(b.status)] - ORDER[healthOf(a.status)])[0];
}

/** `L4TOUT · 3 min ago`: why the last check failed, and how long since the state changed. */
export function checkText(
  r: Pick<MemberRow, "check_status" | "last_check" | "last_change_s">,
): string {
  const parts: string[] = [];
  if (r.check_status) parts.push(r.check_status);
  if (r.last_check) parts.push(r.last_check);
  const s = r.last_change_s;
  if (s != null) parts.push(ago(s));
  return parts.join(" · ");
}

export function ago(seconds: number): string {
  if (seconds < 90) return `${Math.max(0, Math.round(seconds))} s ago`;
  const m = Math.round(seconds / 60);
  if (m < 90) return `${m} min ago`;
  const h = Math.round(m / 60);
  return h < 36 ? `${h} h ago` : `${Math.round(h / 24)} d ago`;
}

/** Rows of one member (by pool and member id) out of a service's status. */
export function rowsFor(
  members: MemberRow[] | undefined,
  poolId: string,
  memberId: string,
): MemberRow[] {
  return (members ?? []).filter((m) => m.pool_id === poolId && m.member_id === memberId);
}
