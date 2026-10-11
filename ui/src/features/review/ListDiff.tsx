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

/** "api.example.com /v2* → pool api-v2", or "→ redirect 301 https://…". */
export function describeRoute(r: RouteLike, labels: Labels): string {
  const m = r.match;
  const path = m.path_exact ? `= ${m.path_exact}` : m.path_prefix ? `${m.path_prefix}*` : "";
  const when = [m.host, path].filter(Boolean).join(" ") || t("any request");
  const to = r.redirect
    ? t("redirect {code} {to}", { code: r.redirect.code, to: r.redirect.location })
    : t("pool {name}", { name: (r.pool_id && labels[r.pool_id]) || "—" });
  return `${when} → ${to}`;
}

function describeMember(m: Member): string {
  return `${m.name ?? m.address} ${m.address}${m.port ? `:${m.port}` : ""}${m.weight != null ? ` w${m.weight}` : ""}${m.admin_state && m.admin_state !== "enabled" ? ` ${m.admin_state}` : ""}`;
}

function describePort(p: Port): string {
  return `${p.port}${p.tls ? " TLS" : ""}${p.http2 ? " h2" : ""}${p.proxy_protocol ? " PROXY" : ""}`;
}

const TONE: Record<Marker, string> = {
  "+": "bg-diff-add",
  "−": "bg-diff-del",
  "~": "bg-status-degraded-bg",
  "↕": "bg-status-draining-bg",
  " ": "",
};
const LABEL: Record<Marker, string> = {
  "+": "added",
  "−": "removed",
  "~": "changed",
  "↕": "moved",
  " ": "",
};

function Mark({ m }: { m: Marker }) {
  return (
    <span className="w-5 shrink-0 text-center" title={t(LABEL[m])}>
      {m}
      <span className="sr-only">{t(LABEL[m])}</span>
    </span>
  );
}

function OrderedList<T>({ rows, text }: { rows: OrderedRow<T>[]; text: (x: T) => string }) {
  return (
    <ol className="divide-y font-mono text-xs">
      {rows.map((r, i) => (
        <li key={i} data-marker={r.marker} className={cn("flex gap-3 px-4 py-1.5", TONE[r.marker])}>
          <Mark m={r.marker} />
          <span className="w-6 shrink-0 text-muted-foreground">{r.position}</span>
          <span>
            {r.marker === "~" && r.before !== undefined && (
              <span className="mr-2 text-muted-foreground line-through">{text(r.before)}</span>
            )}
            {text((r.after ?? r.before) as T)}
          </span>
        </li>
      ))}
    </ol>
  );
}

function KeyedList<T>({
  diff,
  text,
}: {
  diff: { rows: KeyedRow<T>[]; unchanged: number };
  text: (x: T) => string;
}) {
  return (
    <ul className="divide-y font-mono text-xs">
      {diff.rows.map((r) => (
        <li
          key={`${r.marker}-${r.key}`}
          data-marker={r.marker}
          className={cn("flex gap-3 px-4 py-1.5", TONE[r.marker])}
        >
          <Mark m={r.marker} />
          <span>
            {r.marker === "~" && r.before !== undefined && (
              <span className="mr-2 text-muted-foreground line-through">{text(r.before)}</span>
            )}
            {text((r.after ?? r.before) as T)}
          </span>
        </li>
      ))}
      {diff.unchanged > 0 && (
        <li className="px-4 py-1.5 text-muted-foreground">
          {t("{n} unchanged", { n: diff.unchanged })}
        </li>
      )}
    </ul>
  );
}

/**
 * One list field of a changed object: routes as a numbered ordered diff, members, ports,
 * certificates and policy sets as a keyed diff with unchanged rows collapsed.
 */
export function ListDiff({
  field,
  before,
  after,
  labels,
}: {
  field: string;
  before: unknown;
  after: unknown;
  labels: Labels;
}) {
  const b = (Array.isArray(before) ? before : []) as unknown[];
  const a = (Array.isArray(after) ? after : []) as unknown[];
  if (field === "routes")
    return (
      <OrderedList
        rows={diffOrdered(b as RouteLike[], a as RouteLike[], routeKey)}
        text={(r) => describeRoute(r, labels)}
      />
    );
  if (field === "members")
    return (
      <KeyedList diff={diffKeyed(b as Member[], a as Member[], memberKey)} text={describeMember} />
    );
  if (field === "ports")
    return (
      <KeyedList
        diff={diffKeyed(b as Port[], a as Port[], (p) => String(p.port))}
        text={describePort}
      />
    );
  const name = (id: string) => labels[id] ?? id;
  return <KeyedList diff={diffKeyed(b as string[], a as string[], name)} text={name} />;
}
