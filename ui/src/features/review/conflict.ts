/**
 * Three-way conflict on apply (`412` with `conflict: {object_type, object_id, expected_version,
 * current_version}`). The API sends versions only, so the UI reads the base revision's snapshot
 * (what the draft started from), the current object (theirs) and the draft object (yours) and
 * works out which fields really conflict.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, rawGet, unwrap } from "@/api/client";
import { mergeAdds, onlyAdds } from "@/lib/list-diff";
import { TYPE_PATH } from "./model";

type Json = unknown;
type Obj = Record<string, Json>;

export interface Conflict {
  object_type: string;
  object_id: string;
  expected_version: number;
  current_version: number;
}

export type Choice = "base" | "theirs" | "yours" | "both";

export interface FieldConflict {
  field: string;
  base: Json;
  theirs: Json;
  yours: Json;
  /** The merge of two sides that each only added list items, theirs first; null when not possible. */
  both: Json[] | null;
}

const same = (a: Json, b: Json) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
const IGNORED = new Set(["id", "version", "tenant_id", "changed_fields", "draft_status"]);
const keyOf = (x: Json) => (typeof x === "string" ? x : JSON.stringify(x));

/** Snapshot collection of an object type, for example `virtual_service` to `virtual_services`. */
export function collectionOf(type: string): string {
  return TYPE_PATH[type]?.replace(/-/g, "_") ?? type;
}

/**
 * Fields changed by both sides to different values. Fields only one side changed merge on their
 * own (yours keep, theirs are already live) and are returned in `yoursOnly`.
 */
export function computeConflicts(base: Obj | undefined, theirs: Obj, yours: Obj) {
  const keys = new Set([...Object.keys(base ?? {}), ...Object.keys(theirs), ...Object.keys(yours)]);
  const conflicts: FieldConflict[] = [];
  const yoursOnly: Obj = {};
  for (const field of [...keys].sort()) {
    if (IGNORED.has(field)) continue;
    const b = base?.[field];
    const t = theirs[field];
    const y = yours[field];
    const yoursChanged = !same(y, b);
    const theirsChanged = !same(t, b);
    if (yoursChanged && !theirsChanged) yoursOnly[field] = y;
    else if (yoursChanged && theirsChanged && !same(t, y)) {
      const lists = Array.isArray(b) && Array.isArray(t) && Array.isArray(y);
      const both =
        lists && onlyAdds(b, t, keyOf) && onlyAdds(b, y, keyOf) ? mergeAdds(b, t, y, keyOf) : null;
      conflicts.push({ field, base: b, theirs: t, yours: y, both });
    }
  }
  return { conflicts, yoursOnly };
}

/** The value a choice stands for. */
export function valueFor(c: FieldConflict, choice: Choice): Json {
  return choice === "base"
    ? c.base
    : choice === "theirs"
      ? c.theirs
      : choice === "both"
        ? c.both
        : c.yours;
}

/** The three versions of the conflicting object: base revision, current, and the draft. */
export function useConflictData(
  tenant: string,
  cs: string,
  baseRevision: number,
  c: Conflict | undefined,
) {
  return useQuery({
    queryKey: ["t", tenant, "change-sets", cs, "conflict", c?.object_id, c?.current_version],
    enabled: !!c,
    queryFn: async () => {
      const path = `/api/v1/tenants/${encodeURIComponent(tenant)}/${TYPE_PATH[c!.object_type]}/${c!.object_id}`;
      const [rev, theirs, yours] = await Promise.all([
        baseRevision > 0
          ? unwrap(
              await api.GET("/api/v1/tenants/{t}/revisions/{n}", {
                params: { path: { t: tenant, n: baseRevision } },
              }),
            )
          : Promise.resolve(undefined),
        rawGet<Obj>(path),
        rawGet<Obj>(`${path}?change_set=${cs}`),
      ]);
      const list =
        (rev?.snapshot as Record<string, Obj[] | undefined> | undefined)?.[
          collectionOf(c!.object_type)
        ] ?? [];
      const base = list.find((o) => String(o.id) === c!.object_id);
      return { base, theirs, yours, ...computeConflicts(base, theirs, yours) };
    },
  });
}

/**
 * Apply the choices to the draft: drop the stale op for this object and add one update op with
 * your non-conflicting changes plus the chosen values, expecting the current version.
 */
export function useResolveConflict(tenant: string, cs: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: {
      conflict: Conflict;
      opIndexes: number[];
      yoursOnly: Obj;
      choices: Record<string, { choice: Choice; value: Json }>;
    }) => {
      for (const index of [...v.opIndexes].sort((a, b) => b - a))
        unwrap(
          await api.DELETE("/api/v1/tenants/{t}/change-sets/{cs}/ops/{index}", {
            params: { path: { t: tenant, cs, index } },
          }),
        );
      const body: Obj = { ...v.yoursOnly };
      for (const [field, { value }] of Object.entries(v.choices)) body[field] = value;
      if (Object.keys(body).length > 0)
        unwrap(
          await api.POST("/api/v1/tenants/{t}/change-sets/{cs}/ops", {
            params: { path: { t: tenant, cs } },
            body: {
              ops: [
                {
                  action: "update",
                  object_type: v.conflict.object_type as never,
                  object_id: v.conflict.object_id,
                  body,
                  expected_version: v.conflict.current_version,
                },
              ],
            },
          }),
        );
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ["t", tenant] }),
  });
}
