/**
 * Metrics on the VS Overview: four single-series panels drawn in the UI from the tenant-scoped
 * metrics API (never a Grafana iframe, which would let a viewer change the tenant variable).
 * "Open in Grafana" stays as a link. Missing data is a gap, never 0.
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { ExternalLink, RefreshCw } from "lucide-react";
import { useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import { useMe } from "@/auth/session";
import { TimeSeries, type Point } from "@/components/sy/TimeSeries";
import { UnavailableState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const RANGES: [number, string][] = [
  [900, "15 m"],
  [3600, "1 h"],
  [21600, "6 h"],
  [86400, "24 h"],
  [604800, "7 d"],
];
const REFRESH: [number, string][] = [
  [0, "Refresh off"],
  [15, "Refresh every 15 s"],
  [30, "Refresh every 30 s"],
  [60, "Refresh every 1 min"],
];

function useSeries(tenant: string, id: string, windowS: number, refreshS: number) {
  return useQuery({
    queryKey: ["t", tenant, "metrics", "vs", id, { windowS }],
    placeholderData: keepPreviousData,
    refetchInterval: refreshS > 0 ? refreshS * 1000 : false,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/virtual-services/{object_id}/metrics", {
          params: { path: { t: tenant, object_id: id }, query: { window_s: windowS } },
        }),
      ),
  });
}

/** 1,234 / 56 / 7.5: whole numbers from 10 up, one decimal below. */
export function fmt(v: number): string {
  if (v >= 100) return Math.round(v).toLocaleString();
  if (v >= 10) return v.toFixed(0);
  return v.toFixed(1).replace(/\.0$/, "");
}

const PANELS = [
  { key: "rps", title: "Requests", unit: "per second", scale: 1 },
  { key: "p95_ms", title: "Latency p95", unit: "ms", scale: 1 },
  { key: "error_5xx_ratio", title: "Server errors (5xx)", unit: "% of responses", scale: 100 },
  { key: "active_connections", title: "Active connections", unit: "count", scale: 1 },
] as const;

function offsetLabel(): string {
  const m = -new Date().getTimezoneOffset();
  const sign = m < 0 ? "−" : "+";
  const h = Math.floor(Math.abs(m) / 60);
  const min = Math.abs(m) % 60;
  return `UTC${sign}${h}${min ? `:${String(min).padStart(2, "0")}` : ""}`;
}

export function VsMetrics({
  tenant,
  id,
  groupName,
}: {
  tenant: string;
  id: string;
  groupName?: string;
}) {
  const me = useMe();
  const [windowS, setWindowS] = useState(3600);
  const [refreshS, setRefreshS] = useState(30);
  const [table, setTable] = useState(false);
  const q = useSeries(tenant, id, windowS, refreshS);
  const grafana = me.data?.links?.grafana;
  const m = q.data?.available ? q.data.metrics : null;
  const rangeLabel = RANGES.find(([s]) => s === windowS)?.[1] ?? "";

  let body;
  if (q.isPending) {
    body = (
      <div role="status" aria-label={t("Loading metrics")} className="grid gap-4 sm:grid-cols-2">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="h-48 animate-pulse rounded-lg border bg-muted/60" />
        ))}
      </div>
    );
  } else if (q.isError) {
    const err = q.error;
    if (err instanceof ApiError && err.status === 403) {
      const role = err.problem?.required_role;
      body = (
        <UnavailableState
          title={t("No access to metrics")}
          reason={`${err.problem?.detail ?? ""}${role ? ` (${t("needs {role}", { role })})` : ""}`}
        />
      );
    } else {
      body = (
        <div role="alert" className="rounded-lg border border-status-down/30 p-4 text-sm">
          {err.message}{" "}
          <Button size="sm" variant="outline" onClick={() => void q.refetch()}>
            {t("Retry")}
          </Button>
        </div>
      );
    }
  } else if (!q.data.available) {
    body = <UnavailableState title={t("Metrics unavailable")} reason={q.data.reason} />;
  } else if (!m || PANELS.every((p) => m[p.key].length === 0)) {
    body = <UnavailableState title={t("No data for this period")} />;
  } else if (table) {
    body = (
      <div className="max-h-96 overflow-auto rounded-lg border">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-muted text-left text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">{t("Time")}</th>
              {PANELS.map((p) => (
                <th key={p.key} className="px-3 py-2 font-medium">
                  {t(p.title)} ({p.unit})
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y font-mono">
            {m.rps.map((p, i) => (
              <tr key={p.t}>
                <td className="px-3 py-1.5">{new Date(p.t).toLocaleTimeString()}</td>
                {PANELS.map((pn) => {
                  const c: Point | undefined = m[pn.key][i];
                  return (
                    <td key={pn.key} className="px-3 py-1.5">
                      {c ? fmt(c.v * pn.scale) : "—"}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  } else {
    body = (
      <div className="grid gap-4 sm:grid-cols-2">
        {PANELS.map((p) => {
          const pts = m[p.key].map((x) => ({ ...x, v: x.v * p.scale }));
          const now = m.summary[p.key];
          return (
            <section key={p.key} className="rounded-lg border bg-card p-3">
              <header className="mb-1 flex items-baseline gap-2 text-sm">
                <h3 className="font-semibold whitespace-nowrap">{t(p.title)}</h3>
                <span className="truncate text-xs text-muted-foreground">{p.unit}</span>
                <span className="ml-auto font-mono text-xs">
                  {now == null ? "—" : fmt(now * p.scale)}{" "}
                  <span className="font-sans text-muted-foreground">{t("now")}</span>
                </span>
              </header>
              <TimeSeries
                points={pts}
                stepS={m.step_s}
                windowS={m.window_s}
                label={`${t(p.title)}, ${p.unit}`}
                format={fmt}
                unit={p.unit}
              />
            </section>
          );
        })}
      </div>
    );
  }

  return (
    <section className="space-y-3 rounded-lg border bg-card p-4" aria-labelledby="metrics-h">
      <header className="flex flex-wrap items-center gap-3">
        <h2 id="metrics-h" className="text-base font-semibold">
          {t("Metrics")}
        </h2>
        <span className="text-xs text-muted-foreground">
          {groupName
            ? t("this service, summed over the engines of {group}", { group: groupName })
            : t("this service")}
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <div
            role="group"
            aria-label={t("Time range")}
            className="flex overflow-hidden rounded-md border"
          >
            {RANGES.map(([s, label]) => (
              <button
                key={s}
                type="button"
                aria-pressed={windowS === s}
                onClick={() => setWindowS(s)}
                className={cn(
                  "border-r px-2.5 py-1.5 text-sm last:border-r-0",
                  windowS === s ? "bg-accent font-medium" : "hover:bg-muted",
                )}
              >
                {label}
              </button>
            ))}
          </div>
          <select
            aria-label={t("Auto-refresh")}
            className="h-9 rounded-md border border-input bg-card px-3 text-sm"
            value={refreshS}
            onChange={(e) => setRefreshS(Number(e.target.value))}
          >
            {REFRESH.map(([s, label]) => (
              <option key={s} value={s}>
                {t(label)}
              </option>
            ))}
          </select>
          <Button
            variant="outline"
            size="icon"
            aria-label={t("Refresh metrics")}
            onClick={() => void q.refetch()}
          >
            <RefreshCw className={cn("size-4", q.isFetching && "animate-spin")} aria-hidden />
          </Button>
        </div>
      </header>
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
        {grafana && (
          <a
            href={grafana}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-sm text-primary hover:underline"
          >
            <ExternalLink className="size-3.5" aria-hidden /> {t("Open in Grafana")}
          </a>
        )}
        {m && (
          <span>
            {t("Last {range}, {step} s steps · times in your browser's zone ({zone})", {
              range: rangeLabel,
              step: m.step_s,
              zone: offsetLabel(),
            })}
          </span>
        )}
        {m && (
          <button
            type="button"
            className="ml-auto text-sm text-primary hover:underline"
            onClick={() => setTable(!table)}
          >
            {table ? t("Show charts") : t("Table view")}
          </button>
        )}
      </div>
      {body}
      {m && !table && (
        <p className="text-xs text-muted-foreground">
          {t(
            "The grey band is a gap with no samples; lines break there instead of joining. Hover or use the arrow keys for the value at a time; the table view lists the same samples.",
          )}
        </p>
      )}
    </section>
  );
}
