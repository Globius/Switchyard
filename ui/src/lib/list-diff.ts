// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Diffs of list fields for the Review screen.
 *
 * Ordered lists (routes) keep their positions: rows are numbered from 1 and marked `+` added,
 * `−` removed, `~` changed in place, `↕` moved. Unordered lists (members, certificates) are
 * matched by a key and unchanged rows are counted, not listed.
 */

export type Marker = "+" | "−" | "~" | "↕" | " ";

export interface OrderedRow<T> {
  marker: Marker;
  /** 1-based position in the draft (or in live, for removed rows). */
  position: number;
  before?: T;
  after?: T;
}

export interface KeyedRow<T> {
  marker: Marker;
  key: string;
  before?: T;
  after?: T;
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

/**
 * Ordered diff. Items are matched by `key`; an item whose relative order among the matched items
 * changed is `↕`, one whose content changed is `~` (moved and changed shows `~`).
 *
 * @example
 * diffOrdered([a, b], [b, a], (r) => r.host) // both rows ↕
 */
export function diffOrdered<T>(before: T[], after: T[], key: (item: T) => string): OrderedRow<T>[] {
  const bKeys = before.map(key);
  const aKeys = after.map(key);
  const common = aKeys.filter((k) => bKeys.includes(k));
  const commonBefore = bKeys.filter((k) => aKeys.includes(k));
  const rows: OrderedRow<T>[] = after.map((item, i) => {
    const k = aKeys[i]!;
    const bi = bKeys.indexOf(k);
    if (bi < 0) return { marker: "+" as const, position: i + 1, after: item };
    const prev = before[bi]!;
    const moved = common.indexOf(k) !== commonBefore.indexOf(k);
    const marker: Marker = !same(prev, item) ? "~" : moved ? "↕" : " ";
    return { marker, position: i + 1, before: prev, after: item };
  });
  before.forEach((item, i) => {
    if (!aKeys.includes(bKeys[i]!)) rows.push({ marker: "−", position: i + 1, before: item });
  });
  return rows;
}

/** Keyed diff for unordered lists; `unchanged` counts the rows left out. */
export function diffKeyed<T>(
  before: T[],
  after: T[],
  key: (item: T) => string,
): { rows: KeyedRow<T>[]; unchanged: number } {
  const b = new Map(before.map((x) => [key(x), x]));
  const a = new Map(after.map((x) => [key(x), x]));
  const rows: KeyedRow<T>[] = [];
  let unchanged = 0;
  for (const [k, item] of a) {
    const prev = b.get(k);
    if (!prev) rows.push({ marker: "+", key: k, after: item });
    else if (!same(prev, item)) rows.push({ marker: "~", key: k, before: prev, after: item });
    else unchanged++;
  }
  for (const [k, item] of b) if (!a.has(k)) rows.push({ marker: "−", key: k, before: item });
  return { rows, unchanged };
}

/** True when every difference between the two lists is an addition. */
export function onlyAdds<T>(base: T[], side: T[], key: (item: T) => string): boolean {
  const s = new Set(side.map(key));
  return base.every((x) => s.has(key(x)));
}

/**
 * Three-way merge of a list where each side only added items: base, then theirs first, then
 * yours. Returns null when either side removed or changed something (no "Both" choice then).
 */
export function mergeAdds<T>(
  base: T[],
  theirs: T[],
  yours: T[],
  key: (item: T) => string,
): T[] | null {
  const bk = new Map(base.map((x) => [key(x), x]));
  const ok = (side: T[]) =>
    base.every((x) => side.some((y) => key(y) === key(x) && same(y, bk.get(key(x)))));
  if (!ok(theirs) || !ok(yours)) return null;
  const seen = new Set(base.map(key));
  const out = [...base];
  for (const x of [...theirs, ...yours]) {
    if (!seen.has(key(x))) {
      seen.add(key(x));
      out.push(x);
    }
  }
  return out;
}

export interface RouteLike {
  match: { host?: string | null; path_prefix?: string | null; path_exact?: string | null };
  pool_id?: string | null;
  redirect?: { location: string; code: number } | null;
}

/** Routes are matched by host, path and match type (not by position or target). */
export function routeKey(r: RouteLike): string {
  const kind = r.match.path_exact ? "exact" : r.match.path_prefix ? "prefix" : "any";
  return `${r.match.host ?? "*"} ${kind} ${r.match.path_exact ?? r.match.path_prefix ?? ""}`.trim();
}

/** Members are matched by name and port. */
export function memberKey(m: {
  name?: string | null;
  address: string;
  port?: number | null;
}): string {
  return `${m.name ?? m.address}:${m.port ?? ""}`;
}
