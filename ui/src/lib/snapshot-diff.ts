/**
 * Field-level diff of two desired-state snapshots (revisions) or of live and drafted objects.
 * Compared in the browser, so secrets never appear: objects carry only `secret_ref`s.
 */

type Json = unknown;
export type Collections = Record<string, Record<string, Json>[] | undefined>;

export interface FieldChange {
  field: string;
  before: Json;
  after: Json;
}

export interface ObjectChange {
  /** Snapshot collection, for example `virtual_services`. */
  collection: string;
  id: string;
  name: string;
  status: "created" | "modified" | "deleted";
  fields: FieldChange[];
}

/** Server bookkeeping that is not part of what a person changed. */
const IGNORED = new Set(["id", "version", "tenant_id", "changed_fields", "draft_status"]);

const same = (a: Json, b: Json) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

/** Changed fields of one object; `before` undefined means the object is new. */
export function diffObject(
  before: Record<string, Json> | undefined,
  after: Record<string, Json> | undefined,
): FieldChange[] {
  const keys = new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})]);
  const out: FieldChange[] = [];
  for (const k of [...keys].sort()) {
    if (IGNORED.has(k)) continue;
    if (!same(before?.[k], after?.[k]))
      out.push({ field: k, before: before?.[k], after: after?.[k] });
  }
  return out;
}

/**
 * Objects created, modified or deleted between two snapshots, sorted by collection then name.
 *
 * @example
 * diffSnapshots({ pools: [{ id: "1", name: "a", algorithm: "roundrobin" }] },
 *               { pools: [{ id: "1", name: "a", algorithm: "leastconn" }] });
 * // [{ collection: "pools", status: "modified", fields: [{ field: "algorithm", ... }], ... }]
 */
export function diffSnapshots(from: Collections, to: Collections): ObjectChange[] {
  const out: ObjectChange[] = [];
  for (const collection of new Set([...Object.keys(from), ...Object.keys(to)])) {
    const a = new Map((from[collection] ?? []).map((o) => [String(o.id), o]));
    const b = new Map((to[collection] ?? []).map((o) => [String(o.id), o]));
    for (const [id, after] of b) {
      const before = a.get(id);
      if (!before) {
        out.push({
          collection,
          id,
          name: String(after.name ?? id),
          status: "created",
          fields: diffObject(undefined, after),
        });
        continue;
      }
      const fields = diffObject(before, after);
      if (fields.length)
        out.push({ collection, id, name: String(after.name ?? id), status: "modified", fields });
    }
    for (const [id, before] of a) {
      if (!b.has(id))
        out.push({
          collection,
          id,
          name: String(before.name ?? id),
          status: "deleted",
          fields: diffObject(before, undefined),
        });
    }
  }
  return out.sort(
    (x, y) => x.collection.localeCompare(y.collection) || x.name.localeCompare(y.name),
  );
}

/** Display text for a value: scalars as is, lists and objects as compact JSON. */
export function show(v: Json): string {
  if (v === undefined || v === null) return "—";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

const LABEL: Record<string, string> = {
  networks: "Network",
  vip_pools: "VIP pool",
  se_groups: "SE group",
  virtual_services: "Virtual service",
  pools: "Pool",
  health_monitors: "Health monitor",
  certificates: "Certificate",
  policy_sets: "Policy set",
  routing_profiles: "Routing profile",
  tenants: "Tenant",
};

export function collectionLabel(c: string): string {
  return LABEL[c] ?? c;
}
