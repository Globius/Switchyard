// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * The Metrics section (design v52): four single-series panels drawn in the UI from the
 * tenant-scoped metrics API (never a Grafana iframe, which would let a viewer change the tenant
 * variable). {@link VsMetrics} is the one on the VS Overview; `EngineMetrics` (SE groups) uses the
 * same {@link MetricsView}. "Open in Grafana" stays a link. Missing data is a gap, never 0.
 */
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { TimeSeries, type Point } from "@/components/sy/TimeSeries";
import { UnavailableState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { GrafanaLink } from "./GrafanaLink";

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

/** 1,234 / 56 / 7.5: whole numbers from 10 up, one decimal below. */
export function fmt(v: number): string {
  if (v >= 100) return Math.round(v).toLocaleString();
  if (v >= 10) return v.toFixed(0);
  return v.toFixed(1).replace(/\.0$/, "");
}

function offsetLabel(): string {
  const m = -new Date().getTimezoneOffset();
  const sign = m < 0 ? "−" : "+";
  const h = Math.floor(Math.abs(m) / 60);
  const min = Math.abs(m) % 60;
  return `UTC${sign}${h}${min ? `:${String(min).padStart(2, "0")}` : ""}`;
}

/** One panel, already scaled to the unit it shows. */
export interface PanelSpec {
  key: string;
  title: string;
  unit: string;
  points: Point[];
  /** The API's latest value, scaled; null or undefined shows "—". */
  now: number | null | undefined;
}

/** What a scope's API answer turns into. */
export interface PanelSet {
  stepS: number;
  windowS: number;
  panels: PanelSpec[];
}

interface Answer<M> {
  available: boolean;
  reason?: string | null;
  metrics?: M | null;
}

/** The section: controls, the four panels (or a state), the table view and the Grafana link. */
export function MetricsView<M>({
  tenant,
  queryKey,
  fetcher,
  toPanels,
  scopeLine,
  grafanaService,
}: {
  tenant: string;
  /** Identifies the scope in the query cache; the range is added. */
  queryKey: unknown[];
  fetcher: (windowS: number) => Promise<Answer<M>>;
  toPanels: (m: M) => PanelSet;
  scopeLine: string;
  /** The service whose Grafana dashboard "Open in Grafana" lands on, if any. */
  grafanaService?: string;
}) {
  const [windowS, setWindowS] = useState(3600);
  const [refreshS, setRefreshS] = useState(30);
  const [table, setTable] = useState(false);
  const q = useQuery({
    queryKey: [...queryKey, { windowS }],
    placeholderData: keepPreviousData,
    refetchInterval: refreshS > 0 ? refreshS * 1000 : false,
    queryFn: () => fetcher(windowS),
  });
  const set = q.data?.available && q.data.metrics ? toPanels(q.data.metrics) : null;
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
  } else if (!set || set.panels.every((p) => p.points.length === 0)) {
    body = <UnavailableState title={t("No data for this period")} />;
  } else if (table) {
    const rows = set.panels[0]!.points;
    body = (
      <div className="max-h-96 overflow-auto rounded-lg border">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-muted text-left text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">{t("Time")}</th>
              {set.panels.map((p) => (
                <th key={p.key} className="px-3 py-2 font-medium">
                  {p.title} ({p.unit})
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y font-mono">
            {rows.map((p, i) => (
              <tr key={p.t}>
                <td className="px-3 py-1.5">{new Date(p.t).toLocaleTimeString()}</td>
                {set.panels.map((pn) => {
                  const c: Point | undefined = pn.points[i];
                  return (
                    <td key={pn.key} className="px-3 py-1.5">
                      {c ? fmt(c.v) : "—"}
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
        {set.panels.map((p) => (
          <section key={p.key} className="rounded-lg border bg-card p-3">
            <header className="mb-1 flex items-baseline gap-2 text-sm">
              <h3 className="font-semibold whitespace-nowrap">{p.title}</h3>
              <span className="truncate text-xs text-muted-foreground">{p.unit}</span>
              <span className="ml-auto font-mono text-xs">
                {p.now == null ? "—" : fmt(p.now)}{" "}
                <span className="font-sans text-muted-foreground">{t("now")}</span>
              </span>
            </header>
            <TimeSeries
              points={p.points}
              stepS={set.stepS}
              windowS={set.windowS}
              label={`${p.title}, ${p.unit}`}
              format={fmt}
              unit={p.unit}
            />
          </section>
        ))}
      </div>
    );
  }

  return (
    <section className="space-y-3 rounded-lg border bg-card p-4" aria-labelledby="metrics-h">
      <header className="flex flex-wrap items-center gap-3">
        <h2 id="metrics-h" className="text-base font-semibold">
          {t("Metrics")}
        </h2>
        <span className="text-xs text-muted-foreground">{scopeLine}</span>
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
        <GrafanaLink tenant={tenant} service={grafanaService} />
        {set && (
          <span>
            {t("Last {range}, {step} s steps · times in your browser's zone ({zone})", {
              range: rangeLabel,
              step: set.stepS,
              zone: offsetLabel(),
            })}
          </span>
        )}
        {set && (
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
      {set && !table && (
        <p className="text-xs text-muted-foreground">
          {t(
            "The grey band is a gap with no samples; lines break there instead of joining. Hover or use the arrow keys for the value at a time; the table view lists the same samples.",
          )}
        </p>
      )}
    </section>
  );
}

type ServiceMetrics = components["schemas"]["ServiceMetrics"];

const VS_PANELS = [
  { key: "rps", title: "Requests", unit: "per second", scale: 1 },
  { key: "p95_ms", title: "Latency p95", unit: "ms", scale: 1 },
  { key: "error_5xx_ratio", title: "Server errors (5xx)", unit: "% of responses", scale: 100 },
  { key: "active_connections", title: "Active connections", unit: "count", scale: 1 },
] as const;

function vsPanels(m: ServiceMetrics): PanelSet {
  return {
    stepS: m.step_s,
    windowS: m.window_s,
    panels: VS_PANELS.map((p) => {
      const now = m.summary[p.key];
      return {
        key: p.key,
        title: t(p.title),
        unit: p.unit,
        points: m[p.key].map((x) => ({ ...x, v: x.v * p.scale })),
        now: now == null ? null : now * p.scale,
      };
    }),
  };
}

export function VsMetrics({
  tenant,
  id,
  name,
  groupName,
}: {
  tenant: string;
  id: string;
  name?: string;
  groupName?: string;
}) {
  return (
    <MetricsView
      tenant={tenant}
      queryKey={["t", tenant, "metrics", "vs", id]}
      fetcher={async (windowS) =>
        unwrap(
          await api.GET("/api/v1/tenants/{t}/virtual-services/{object_id}/metrics", {
            params: { path: { t: tenant, object_id: id }, query: { window_s: windowS } },
          }),
        )
      }
      toPanels={vsPanels}
      scopeLine={
        groupName
          ? t("this service, summed over the engines of {group}", { group: groupName })
          : t("this service")
      }
      grafanaService={name}
    />
  );
}
