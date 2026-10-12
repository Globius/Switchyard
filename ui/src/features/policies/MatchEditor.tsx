/**
 * Request match shared by routes and policy rules (every condition given must hold):
 * host, path, methods, header rows, query rows, source networks and TLS server name.
 * {@link ExtraMatch} is the part beyond host and path, for screens that already edit those two.
 */
import { Plus, X } from "lucide-react";
import type { components } from "@/api/schema";
import { Button } from "@/components/ui/button";
import { cidrProblem } from "@/lib/ip";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export type Match = components["schemas"]["RouteMatch"];
type HeaderMatch = components["schemas"]["HeaderMatch"];
type QueryMatch = components["schemas"]["QueryMatch"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";
export const METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"] as const;
const HEADER_OPS: HeaderMatch["op"][] = [
  "equals",
  "present",
  "absent",
  "prefix",
  "suffix",
  "contains",
];

/** A match with every optional condition empty. */
export function emptyMatch(): Match {
  return { headers: [], methods: [], query: [], source_cidrs: [] };
}

/** Only the conditions that are set, so an update op carries no empty arrays or nulls. */
export function cleanMatch(m: Partial<Match>): Match {
  const out: Record<string, unknown> = {};
  if (m.host) out.host = m.host;
  if (m.path_exact) out.path_exact = m.path_exact;
  if (m.path_prefix) out.path_prefix = m.path_prefix;
  if (m.methods?.length) out.methods = m.methods;
  if (m.headers?.length)
    out.headers = m.headers.map((h) =>
      h.op === "present" || h.op === "absent" ? { name: h.name, op: h.op } : h,
    );
  if (m.query?.length)
    out.query = m.query.map((q) => (q.op === "present" ? { name: q.name, op: q.op } : q));
  if (m.source_cidrs?.length) out.source_cidrs = m.source_cidrs;
  if (m.sni) out.sni = m.sni;
  return out as Match;
}

/** One line per condition, for lists: `GET /api · X-Env equals canary · from 10.0.0.0/8`. */
export function describeMatch(m: Partial<Match>): string {
  const parts: string[] = [];
  if (m.methods?.length) parts.push(m.methods.join("/"));
  if (m.host) parts.push(m.host);
  if (m.path_exact) parts.push(m.path_exact);
  else if (m.path_prefix) parts.push(`${m.path_prefix}*`);
  for (const h of m.headers ?? []) parts.push(`${h.name} ${h.op}${h.value ? ` ${h.value}` : ""}`);
  for (const q of m.query ?? [])
    parts.push(`?${q.name}${q.op === "equals" ? `=${q.value ?? ""}` : ""}`);
  if (m.source_cidrs?.length) parts.push(`from ${m.source_cidrs.join(", ")}`);
  if (m.sni) parts.push(`SNI ${m.sni}`);
  return parts.length ? parts.join(" · ") : "every request";
}

/** Parse one network per line; returns the list and the first bad line, if any. */
export function parseNetworks(text: string): { list: string[]; error?: string } {
  const list: string[] = [];
  for (const [i, raw] of text.split("\n").entries()) {
    const l = raw.trim();
    if (!l) continue;
    if (cidrProblem(l)) return { list, error: `Line ${i + 1}: not a valid address or network` };
    list.push(l);
  }
  return { list };
}

function Rows<T>({
  rows,
  onChange,
  blank,
  addLabel,
  render,
  disabled,
}: {
  rows: T[];
  onChange: (r: T[]) => void;
  blank: T;
  addLabel: string;
  render: (row: T, set: (p: Partial<T>) => void, i: number) => React.ReactNode;
  disabled?: boolean;
}) {
  return (
    <div className="space-y-2">
      {rows.map((row, i) => (
        <div key={i} className="flex items-start gap-2">
          <div className="grid flex-1 grid-cols-[1fr_8rem_1fr] gap-2">
            {render(row, (p) => onChange(rows.map((x, j) => (j === i ? { ...x, ...p } : x))), i)}
          </div>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={disabled}
            aria-label={`${t("Remove")} ${i + 1}`}
            onClick={() => onChange(rows.filter((_, j) => j !== i))}
          >
            <X className="size-4" />
          </Button>
        </div>
      ))}
      <Button
        type="button"
        size="sm"
        variant="outline"
        disabled={disabled}
        onClick={() => onChange([...rows, blank])}
      >
        <Plus className="size-4" aria-hidden /> {addLabel}
      </Button>
    </div>
  );
}

/** Methods, headers, query, source networks and SNI. `id` makes the field names unique per rule. */
export function ExtraMatch({
  match,
  onChange,
  disabled,
  id,
}: {
  match: Match;
  onChange: (m: Match) => void;
  disabled?: boolean;
  id: string;
}) {
  const set = (p: Partial<Match>) => onChange({ ...match, ...p });
  const label = "mb-1.5 block text-sm font-medium";
  const nets = parseNetworks((match.source_cidrs ?? []).join("\n"));
  const count =
    (match.methods?.length ? 1 : 0) +
    (match.headers?.length ?? 0) +
    (match.query?.length ?? 0) +
    (match.source_cidrs?.length ? 1 : 0) +
    (match.sni ? 1 : 0);
  return (
    <details className="rounded-md border" open={count > 0}>
      <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
        {t("More conditions")}
        {count > 0 && <span className="ml-2 text-xs text-muted-foreground">({count})</span>}
      </summary>
      <div className="space-y-4 border-t p-3">
        <fieldset className="space-y-1.5">
          <legend className={label}>{t("Method is one of")}</legend>
          <div className="flex flex-wrap gap-3 text-sm">
            {METHODS.map((m) => (
              <label key={m} className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  disabled={disabled}
                  aria-label={`${id} ${m}`}
                  checked={match.methods?.includes(m) ?? false}
                  onChange={(e) =>
                    set({
                      methods: e.target.checked
                        ? [...(match.methods ?? []), m]
                        : (match.methods ?? []).filter((x) => x !== m),
                    })
                  }
                />
                {m}
              </label>
            ))}
          </div>
        </fieldset>
        <div>
          <span className={label}>{t("Request headers")}</span>
          <Rows<HeaderMatch>
            rows={match.headers ?? []}
            onChange={(headers) => set({ headers })}
            blank={{ name: "", op: "equals", value: "" }}
            addLabel={t("Add header condition")}
            disabled={disabled}
            render={(h, upd, i) => (
              <>
                <input
                  aria-label={`${id} header ${i + 1} name`}
                  placeholder="X-Env"
                  className={cn(INPUT, "font-mono")}
                  disabled={disabled}
                  value={h.name}
                  onChange={(e) => upd({ name: e.target.value })}
                />
                <select
                  aria-label={`${id} header ${i + 1} operator`}
                  className={INPUT}
                  disabled={disabled}
                  value={h.op ?? "equals"}
                  onChange={(e) => upd({ op: e.target.value as HeaderMatch["op"] })}
                >
                  {HEADER_OPS.map((o) => (
                    <option key={o} value={o}>
                      {t(o)}
                    </option>
                  ))}
                </select>
                <input
                  aria-label={`${id} header ${i + 1} value`}
                  className={cn(INPUT, "font-mono")}
                  disabled={disabled || h.op === "present" || h.op === "absent"}
                  value={h.value ?? ""}
                  onChange={(e) => upd({ value: e.target.value })}
                />
              </>
            )}
          />
        </div>
        <div>
          <span className={label}>{t("Query parameters")}</span>
          <Rows<QueryMatch>
            rows={match.query ?? []}
            onChange={(query) => set({ query })}
            blank={{ name: "", op: "equals", value: "" }}
            addLabel={t("Add query condition")}
            disabled={disabled}
            render={(q, upd, i) => (
              <>
                <input
                  aria-label={`${id} query ${i + 1} name`}
                  placeholder="utm_source"
                  className={cn(INPUT, "font-mono")}
                  disabled={disabled}
                  value={q.name}
                  onChange={(e) => upd({ name: e.target.value })}
                />
                <select
                  aria-label={`${id} query ${i + 1} operator`}
                  className={INPUT}
                  disabled={disabled}
                  value={q.op ?? "equals"}
                  onChange={(e) => upd({ op: e.target.value as QueryMatch["op"] })}
                >
                  <option value="equals">{t("equals")}</option>
                  <option value="present">{t("present")}</option>
                </select>
                <input
                  aria-label={`${id} query ${i + 1} value`}
                  className={cn(INPUT, "font-mono")}
                  disabled={disabled || q.op === "present"}
                  value={q.value ?? ""}
                  onChange={(e) => upd({ value: e.target.value })}
                />
              </>
            )}
          />
        </div>
        <label className="block">
          <span className={label}>{t("Source networks")}</span>
          <textarea
            aria-label={`${id} source networks`}
            className={cn(INPUT, "h-20 py-2 font-mono text-xs")}
            disabled={disabled}
            placeholder={"10.0.0.0/8\n2001:db8::/32"}
            defaultValue={(match.source_cidrs ?? []).join("\n")}
            onChange={(e) => set({ source_cidrs: parseNetworks(e.target.value).list })}
          />
          <span className="block text-xs text-muted-foreground">
            {t(
              "One IPv4 or IPv6 network per line. The client address after client-IP restoration.",
            )}
          </span>
          {nets.error && (
            <span role="alert" className="block text-xs text-status-down">
              {nets.error}
            </span>
          )}
        </label>
        <label className="block">
          <span className={label}>{t("TLS server name (SNI)")}</span>
          <input
            aria-label={`${id} SNI`}
            className={cn(INPUT, "font-mono")}
            disabled={disabled}
            placeholder="*.example.com"
            value={match.sni ?? ""}
            onChange={(e) => set({ sni: e.target.value || null })}
          />
          <span className="block text-xs text-muted-foreground">
            {t("Never true on a port without TLS.")}
          </span>
        </label>
      </div>
    </details>
  );
}

/** Host, path and the extra conditions: the whole match for a policy rule. */
export function MatchEditor({
  match,
  onChange,
  disabled,
  id,
}: {
  match: Match;
  onChange: (m: Match) => void;
  disabled?: boolean;
  id: string;
}) {
  const mode = match.path_exact ? "exact" : match.path_prefix ? "prefix" : "any";
  const path = match.path_exact ?? match.path_prefix ?? "";
  const label = "mb-1.5 block text-sm font-medium";
  return (
    <div className="space-y-3">
      <div className="grid gap-3 md:grid-cols-[1fr_0.9fr_1.2fr]">
        <label>
          <span className={label}>{t("When host is")}</span>
          <input
            aria-label={`${id} host`}
            className={INPUT}
            disabled={disabled}
            value={match.host ?? ""}
            onChange={(e) => onChange({ ...match, host: e.target.value || null })}
          />
        </label>
        <label>
          <span className={label}>{t("and path")}</span>
          <select
            aria-label={`${id} path match`}
            className={INPUT}
            disabled={disabled}
            value={mode}
            onChange={(e) =>
              onChange({
                ...match,
                path_exact: e.target.value === "exact" ? path || "/" : null,
                path_prefix: e.target.value === "prefix" ? path || "/" : null,
              })
            }
          >
            <option value="any">{t("is anything")}</option>
            <option value="prefix">{t("starts with")}</option>
            <option value="exact">{t("equals")}</option>
          </select>
        </label>
        <label>
          <span className={label}>{t("Path")}</span>
          <input
            aria-label={`${id} path`}
            className={cn(INPUT, "font-mono")}
            disabled={disabled || mode === "any"}
            value={path}
            onChange={(e) =>
              onChange({
                ...match,
                path_exact: mode === "exact" ? e.target.value : null,
                path_prefix: mode === "prefix" ? e.target.value : null,
              })
            }
          />
        </label>
      </div>
      <ExtraMatch match={match} onChange={onChange} disabled={disabled} id={id} />
    </div>
  );
}
