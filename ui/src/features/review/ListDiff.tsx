// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useState } from "react";
import { hostPort } from "@/lib/ip";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import {
  diffKeyed,
  diffOrdered,
  memberKey,
  routeKey,
  type KeyedRow,
  type Marker,
  type OrderedRow,
  type RouteLike,
} from "@/lib/list-diff";

type Labels = Record<string, string>;
type Member = {
  name?: string | null;
  address: string;
  port?: number | null;
  weight?: number;
  admin_state?: string;
};
type Port = { port: number; tls?: boolean; http2?: boolean; proxy_protocol?: boolean };

/** Fields shown as a list diff instead of two JSON blobs. */
export const LIST_FIELDS = [
  "routes",
  "members",
  "ports",
  "certificate_ids",
  "policy_set_ids",
] as const;

export function isListField(field: string, before: unknown, after: unknown): boolean {
  return (
    (LIST_FIELDS as readonly string[]).includes(field) &&
    (Array.isArray(before) || Array.isArray(after))
  );
}

/** "api.example.com prefix /v2/ → api-v2", or "→ redirect 301 https://…". */
export function describeRoute(r: RouteLike, labels: Labels): string {
  const m = r.match;
  const path = m.path_exact
    ? `exact ${m.path_exact}`
    : m.path_prefix
      ? `prefix ${m.path_prefix}`
      : "";
  const when = [m.host, path].filter(Boolean).join(" ") || t("any request");
  const to = r.redirect
    ? t("redirect {code} {to}", { code: r.redirect.code, to: r.redirect.location })
    : (r.pool_id && labels[r.pool_id]) || "—";
  return `${when} → ${to}`;
}

function describeMember(m: Member): string {
  return `${hostPort(m.address, m.port)} · weight ${m.weight ?? "—"} · ${m.admin_state ?? "enabled"}`;
}

function describePort(p: Port): string {
  return `${p.port}${p.tls ? " TLS" : ""}${p.http2 ? " h2" : ""}${p.proxy_protocol ? " PROXY" : ""}`;
}

const WORD: Record<Marker, string> = {
  "+": "added",
  "−": "removed",
  "~": "changed",
  "↕": "moved",
  " ": "same",
};
const PILL: Record<Marker, string> = {
  "+": "bg-diff-add text-status-healthy",
  "−": "bg-diff-del text-status-down",
  "~": "bg-status-degraded-bg text-status-degraded",
  "↕": "bg-status-draining-bg text-status-draining",
  " ": "",
};

/** "3 members → 4 members · 1 added · 3 unchanged (hidden)". */
function summary(
  noun: string,
  before: number,
  after: number,
  counts: Record<string, number>,
  hidden?: number,
) {
  const parts = Object.entries(counts)
    .filter(([, n]) => n > 0)
    .map(([k, n]) => `${n} ${t(k)}`);
  return `${t("{n} {noun}", { n: before, noun })} → ${t("{n} {noun}", { n: after, noun })}${parts.length ? ` · ${parts.join(" · ")}` : ""}${hidden ? ` · ${t("{n} unchanged (hidden)", { n: hidden })}` : ""}`;
}

function OrderedTable<T>({
  rows,
  text,
  revision,
}: {
  rows: OrderedRow<T>[];
  text: (x: T) => string;
  revision?: number;
}) {
  return (
    <table className="w-full border text-xs">
      <thead className="bg-muted/60 text-left text-muted-foreground">
        <tr>
          <th className="w-10 px-3 py-1.5 font-medium">#</th>
          <th className="px-3 py-1.5 font-medium">
            {revision ? t("Live (rev #{n})", { n: revision }) : t("Live")}
          </th>
          <th className="px-3 py-1.5 font-medium">{t("Draft")}</th>
        </tr>
      </thead>
      <tbody className="divide-y font-mono">
        {rows.map((r, i) => (
          <tr key={i} data-marker={r.marker}>
            <td className="px-3 py-1.5 text-muted-foreground">
              {r.position}
              <span className="sr-only">{t(WORD[r.marker])}</span>
            </td>
            <td className={cn("px-3 py-1.5", r.marker === "−" && "bg-diff-del")}>
              {r.before !== undefined ? (
                <span className={cn(r.marker === "~" && "line-through")}>
                  {r.marker === "−" ? "− " : ""}
                  {text(r.before)}
                </span>
              ) : (
                "—"
              )}
            </td>
            <td
              className={cn(
                "px-3 py-1.5",
                r.marker === "+" && "bg-diff-add",
                r.marker === "~" && "bg-status-degraded-bg",
              )}
            >
              {r.marker === " " ? (
                <span className="text-muted-foreground">{t("same")}</span>
              ) : r.marker === "−" ? (
                <span className="text-muted-foreground">{t("removed")}</span>
              ) : r.marker === "↕" ? (
                <span>↕ {t("moved")}</span>
              ) : (
                <span>
                  {r.marker === "+" ? "+ " : "~ "}
                  {text((r.after ?? r.before) as T)}
                </span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function KeyedTable<T>({
  rows,
  unchanged,
  noun,
  text,
  revision,
}: {
  rows: KeyedRow<T>[];
  unchanged: number;
  noun: string;
  text: (x: T) => string;
  revision?: number;
}) {
  const [show, setShow] = useState(false);
  return (
    <div className="space-y-2">
      <table className="w-full border text-xs">
        <thead className="bg-muted/60 text-left text-muted-foreground">
          <tr>
            <th className="px-3 py-1.5 font-medium">{t(noun)}</th>
            <th className="px-3 py-1.5 font-medium">
              {revision ? t("Live (rev #{n})", { n: revision }) : t("Live")}
            </th>
            <th className="px-3 py-1.5 font-medium">{t("Draft")}</th>
            <th className="px-3 py-1.5 font-medium">{t("Change")}</th>
          </tr>
        </thead>
        <tbody className="divide-y font-mono">
          {rows.map((r) => (
            <tr key={`${r.marker}-${r.key}`} data-marker={r.marker}>
              <td className="px-3 py-1.5 font-sans">{r.key}</td>
              <td className={cn("px-3 py-1.5", r.marker === "−" && "bg-diff-del")}>
                {r.before !== undefined ? (
                  <span className={cn(r.marker === "~" && "line-through")}>{text(r.before)}</span>
                ) : (
                  "—"
                )}
              </td>
              <td
                className={cn(
                  "px-3 py-1.5",
                  r.marker === "+" && "bg-diff-add",
                  r.marker === "~" && "bg-status-degraded-bg",
                )}
              >
                {r.after !== undefined ? text(r.after) : "—"}
              </td>
              <td className="px-3 py-1.5 font-sans">
                <span className={cn("inline-flex rounded-full px-2 py-0.5", PILL[r.marker])}>
                  {r.marker} {t(WORD[r.marker])}
                </span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {unchanged > 0 && (
        <button
          type="button"
          className="text-xs text-primary hover:underline"
          onClick={() => setShow(!show)}
        >
          {show ? t("Hide unchanged") : t("Show {n} unchanged", { n: unchanged })}
        </button>
      )}
      {show && (
        <p className="text-xs text-muted-foreground">{t("{n} unchanged", { n: unchanged })}</p>
      )}
    </div>
  );
}

const LEGEND_ORDERED =
  "Markers: + added · − removed · ~ changed (old → new) · ↕ moved (old → new position). Matched by host, path and match type; positions are shown 1-based.";
const LEGEND_KEYED =
  "Members, ports and certificate lists are matched by their key (member name, port number, certificate), so order changes don't show as edits.";

/**
 * One list field of a changed object: routes as a numbered ordered diff, members, ports,
 * certificates and policy sets as a keyed diff with unchanged rows collapsed.
 */
export function ListDiff({
  field,
  before,
  after,
  labels,
  revision,
}: {
  field: string;
  before: unknown;
  after: unknown;
  labels: Labels;
  /** Base revision number for the "Live (rev #N)" column header. */
  revision?: number;
}) {
  const b = (Array.isArray(before) ? before : []) as unknown[];
  const a = (Array.isArray(after) ? after : []) as unknown[];
  if (field === "routes") {
    const rows = diffOrdered(b as RouteLike[], a as RouteLike[], routeKey);
    const count = (m: Marker) => rows.filter((r) => r.marker === m).length;
    return (
      <div className="space-y-2">
        <p className="text-xs">
          {summary("routes", b.length, a.length, {
            added: count("+"),
            removed: count("−"),
            changed: count("~"),
            moved: count("↕"),
          })}
        </p>
        <OrderedTable rows={rows} text={(r) => describeRoute(r, labels)} revision={revision} />
        <p className="text-xs text-muted-foreground">{t(LEGEND_ORDERED)}</p>
      </div>
    );
  }
  const keyed = (() => {
    if (field === "members")
      return {
        noun: "Member",
        d: diffKeyed(b as Member[], a as Member[], memberKey),
        text: describeMember as (x: never) => string,
        plural: "members",
      };
    if (field === "ports")
      return {
        noun: "Port",
        d: diffKeyed(b as Port[], a as Port[], (p) => String(p.port)),
        text: describePort as (x: never) => string,
        plural: "ports",
      };
    const name = (id: string) => labels[id] ?? id;
    return {
      noun: field === "certificate_ids" ? "Certificate" : "Policy set",
      d: diffKeyed(b as string[], a as string[], name),
      text: ((x: string) => name(x)) as (x: never) => string,
      plural: field === "certificate_ids" ? "certificates" : "policy sets",
    };
  })();
  const n = (m: Marker) => keyed.d.rows.filter((r) => r.marker === m).length;
  return (
    <div className="space-y-2">
      <p className="text-xs">
        {summary(
          keyed.plural,
          b.length,
          a.length,
          { added: n("+"), removed: n("−"), changed: n("~") },
          keyed.d.unchanged,
        )}
      </p>
      <KeyedTable
        rows={keyed.d.rows as KeyedRow<never>[]}
        unchanged={keyed.d.unchanged}
        noun={keyed.noun}
        text={keyed.text}
        revision={revision}
      />
      <p className="text-xs text-muted-foreground">{t(LEGEND_KEYED)}</p>
    </div>
  );
}
