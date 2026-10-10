/**
 * Permission helper behind `useCan()`.
 *
 * The matrix mirrors the specification in `docs/design/02-backend-design.md` ("Roles and the
 * permission matrix", ADR 0030). The backend still enforces every check; this only lets the UI
 * disable controls with a tooltip. When `GET /permissions` ships, pass its answer to
 * {@link createCan} instead of {@link DEFAULT_MATRIX}.
 */
import type { components } from "@/api/schema";

export type Role = components["schemas"]["Role"];
type Caller = {
  is_system: boolean;
  tenants: { id: string; role: Role }[];
};

/** Roles weakest to strongest. They nest: each does everything the weaker ones do. */
export const ROLE_ORDER: readonly Role[] = ["viewer", "operator", "tenant-admin", "system-admin"];

/** True when `held` is at least `required`. */
export function roleAtLeast(held: Role | null | undefined, required: Role): boolean {
  if (!held) return false;
  return ROLE_ORDER.indexOf(held) >= ROLE_ORDER.indexOf(required);
}

/** Minimum role per action, keyed by a stable action name. */
export type Matrix = Record<string, Role>;

export const DEFAULT_MATRIX = {
  read: "viewer",
  /** Virtual service, pool and members, health monitor, policy set, certificate record. */
  "app.write": "operator",
  /** Network, VIP pool, SE group, routing profile. */
  "infra.write": "tenant-admin",
  "draft.edit": "operator",
  "approval.request": "operator",
  "approval.decide": "tenant-admin",
  apply: "operator",
  "apply.break_glass": "system-admin",
  rollback: "tenant-admin",
  "member.state": "operator",
  /** Import, issue, renew certificates. */
  "cert.runtime": "operator",
  /** Operators may retry only jobs of actions they could start; the server decides which. */
  "job.retry": "operator",
  "engine.manage": "tenant-admin",
  "require_approval.enable": "tenant-admin",
  "require_approval.disable": "system-admin",
  "quota.change": "system-admin",
  "token.manage": "tenant-admin",
  /** Viewer and operator mappings of the own tenant. */
  "mapping.low": "tenant-admin",
  /** tenant-admin or system-admin mappings, or another tenant's. */
  "mapping.high": "system-admin",
  "users.tenant": "tenant-admin",
  /** Tenants, identity providers, users, system tokens, break-glass. */
  system: "system-admin",
} as const satisfies Matrix;

export type Action = keyof typeof DEFAULT_MATRIX;

/** Result of a permission check; `reason` is a ready-made tooltip when denied. */
export interface Decision {
  allowed: boolean;
  required: Role;
  reason?: string;
}

/** Role the caller holds in `tenantId`; system admins hold `system-admin` everywhere. */
export function roleIn(me: Caller | undefined, tenantId: string | undefined): Role | null {
  if (!me) return null;
  if (me.is_system) return "system-admin";
  return me.tenants.find((x) => x.id === tenantId)?.role ?? null;
}

/**
 * Build a `can(action)` checker for one caller and tenant.
 *
 * @example
 * const can = createCan(me, tenantId);
 * can("apply").allowed; // false for a viewer
 * can("apply").reason; // "Requires the operator role"
 */
export function createCan(
  me: Caller | undefined,
  tenantId: string | undefined,
  matrix: Matrix = DEFAULT_MATRIX,
): (action: Action) => Decision {
  const held = roleIn(me, tenantId);
  return (action) => {
    const required = matrix[action] ?? "system-admin";
    const allowed = roleAtLeast(held, required);
    return allowed
      ? { allowed, required }
      : { allowed, required, reason: `Requires the ${required} role` };
  };
}
