/**
 * `can(verb, object)` from the backend's permission matrix (`GET /permissions`, ADR 0030/0031).
 *
 * The UI never hard-codes rules: it fetches the matrix and compares the caller's role in the
 * tenant with each entry's `min_role`. The backend still enforces every check; this only lets the
 * UI disable controls and show the permission's `description` as the tooltip.
 */
import type { components } from "@/api/schema";

export type Role = components["schemas"]["Role"];
export type Permission = components["schemas"]["PermissionRead"];
export type PermissionMatrix = components["schemas"]["PermissionMatrix"];
export type Verb = Permission["verb"];
export type PermObject = Permission["object"];

interface Caller {
  is_system: boolean;
  tenants: { id: string; role: Role }[];
}

/** True when `held` is at least `required` in the matrix's role order. */
export function roleAtLeast(
  held: Role | null | undefined,
  required: Role,
  roles: readonly Role[],
): boolean {
  if (!held) return false;
  return roles.indexOf(held) >= roles.indexOf(required);
}

/** Role the caller holds in `tenant` (id); system admins hold `system-admin` everywhere. */
export function roleIn(me: Caller | undefined, tenant: string | undefined): Role | null {
  if (!me) return null;
  if (me.is_system) return "system-admin";
  return me.tenants.find((x) => x.id === tenant)?.role ?? null;
}

/** Result of a check; `reason` is a ready-made tooltip when denied. */
export interface Decision {
  allowed: boolean;
  required: Role | null;
  reason?: string;
}

/**
 * Build `can(verb, object)` for one caller and tenant. Denies everything until both the caller
 * and the matrix are known, and denies pairs the matrix does not list.
 *
 * @example
 * const can = createCan(me, tenantId, matrix);
 * can("apply", "change_set").allowed; // false for a viewer
 * can("apply", "change_set").reason; // "Apply a change set when ... (needs operator)"
 */
export function createCan(
  me: Caller | undefined,
  tenant: string | undefined,
  matrix: PermissionMatrix | undefined,
): (verb: Verb, object: PermObject) => Decision {
  const held = roleIn(me, tenant);
  return (verb, object) => {
    const entry = matrix?.permissions.find((p) => p.verb === verb && p.object === object);
    if (!matrix || !entry) return { allowed: false, required: null };
    const allowed = roleAtLeast(held, entry.min_role, matrix.roles);
    return allowed
      ? { allowed, required: entry.min_role }
      : {
          allowed,
          required: entry.min_role,
          reason: `${entry.description} (needs ${entry.min_role})`,
        };
  };
}
