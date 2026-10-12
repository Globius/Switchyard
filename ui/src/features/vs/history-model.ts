// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** The merged timeline of one service: its draft, revisions, runtime actions and approvals. */
import { diffObject, show, type FieldChange } from "@/lib/snapshot-diff";

export type Filter = "all" | "config" | "runtime" | "approvals";

export type Entry =
  | { kind: "draft"; at: string; title: string; author: string; fields: FieldChange[] }
  | { kind: "revision"; at: string; number: number; summary: string; author: string }
  | {
      kind: "runtime" | "approval";
      at: string;
      action: string;
      actor: string;
      object: string | null;
      diff: Record<string, unknown> | null;
    };

export interface AuditLike {
  at: string;
  actor: string;
  action: string;
  object?: string | null;
  diff?: Record<string, unknown> | null;
}

const APPROVAL = /^change_set\.(approve|reject|request_approval)/;
const RUNTIME_CATEGORIES = new Set(["pool_member", "engine", "certificate", "job"]);

/** Which timeline lane an audit event belongs to; plain writes are shown from revisions instead. */
export function laneOf(
  action: string,
  category: string | undefined,
): "approval" | "runtime" | null {
  if (APPROVAL.test(action)) return "approval";
  if (category && RUNTIME_CATEGORIES.has(category)) return "runtime";
  return null;
}

export function buildTimeline(input: {
  draft?: { at: string; title: string; author: string; fields: FieldChange[] } | null;
  revisions: { number: number; summary: string; author: string; created_at: string }[];
  audit: AuditLike[];
  categories: Record<string, string>;
  filter: Filter;
}): Entry[] {
  const out: Entry[] = [];
  const want = (k: Filter) => input.filter === "all" || input.filter === k;
  if (want("config") && input.draft && input.draft.fields.length > 0)
    out.push({ kind: "draft", ...input.draft });
  if (want("config"))
    for (const r of input.revisions)
      out.push({
        kind: "revision",
        at: r.created_at,
        number: r.number,
        summary: r.summary,
        author: r.author,
      });
  const seen = new Set<string>();
  for (const e of input.audit) {
    const key = `${e.at}|${e.actor}|${e.action}|${e.object ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const lane = laneOf(e.action, input.categories[e.action]);
    if (!lane) continue;
    if (!want(lane === "approval" ? "approvals" : "runtime")) continue;
    out.push({
      kind: lane,
      at: e.at,
      action: e.action,
      actor: e.actor,
      object: e.object ?? null,
      diff: e.diff ?? null,
    });
  }
  // The draft is not live yet, so it always sits on top; everything else newest first.
  return out.sort((a, b) => {
    if (a.kind === "draft") return -1;
    if (b.kind === "draft") return 1;
    return b.at.localeCompare(a.at);
  });
}

/** Changed fields of one object between two states, as +/- lines for the diff block. */
export function fieldLines(fields: FieldChange[]): { sign: "+" | "-"; text: string }[] {
  const lines: { sign: "+" | "-"; text: string }[] = [];
  for (const f of fields) {
    if (f.before !== undefined && f.before !== null && show(f.before) !== "—")
      lines.push({ sign: "-", text: `${f.field}: ${show(f.before)}` });
    if (f.after !== undefined && f.after !== null)
      lines.push({ sign: "+", text: `${f.field}: ${show(f.after)}` });
  }
  return lines;
}

/** The service's fields that changed in a revision, from the two snapshots around it. */
export function changesInRevision(
  before: Record<string, unknown> | undefined,
  after: Record<string, unknown> | undefined,
  id: string,
): FieldChange[] {
  const find = (snap: Record<string, unknown> | undefined) =>
    ((snap?.virtual_services as Record<string, unknown>[] | undefined) ?? []).find(
      (o) => String(o.id) === id,
    );
  return diffObject(find(before), find(after));
}

/** `Everything` count line for the empty state. */
export const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "Everything" },
  { key: "config", label: "Config changes" },
  { key: "runtime", label: "Runtime actions" },
  { key: "approvals", label: "Approvals" },
];
