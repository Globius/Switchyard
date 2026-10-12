// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Metrics of one engine (design v52, `Metrics` engine state): CPU, memory, HAProxy sessions and
 * traffic in, opened from "Metrics" in the row's More menu on SE groups. Same section as the VS
 * Overview ({@link MetricsView}); the API enforces the tenant and engine labels.
 */
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { MetricsView, type PanelSet } from "@/features/vs/VsMetrics";
import { t } from "@/lib/i18n";

type EngineMetricsData = components["schemas"]["EngineMetrics"];

const GIB = 2 ** 30;

/** The four engine panels, scaled to the units the design shows (GiB, Mbit/s). */
export function enginePanels(m: EngineMetricsData): PanelSet {
  const s = m.summary;
  const scaled = (pts: EngineMetricsData["cpu_percent"], k: number) =>
    pts.map((p) => ({ ...p, v: p.v * k }));
  const num = (v: number | null | undefined, k: number) => (v == null ? null : v * k);
  const gibTotal = s.memory_total_bytes == null ? null : s.memory_total_bytes / GIB;
  return {
    stepS: m.step_s,
    windowS: m.window_s,
    panels: [
      {
        key: "cpu",
        title: t("CPU"),
        unit: s.vcpus ? t("% of {n} vCPU", { n: s.vcpus }) : t("% of all vCPUs"),
        points: m.cpu_percent,
        now: s.cpu_percent,
      },
      {
        key: "memory",
        title: t("Memory used"),
        unit:
          gibTotal == null
            ? t("GiB")
            : t("GiB of {n}", { n: Number(gibTotal.toFixed(1)).toString() }),
        points: scaled(m.memory_used_bytes, 1 / GIB),
        now: num(s.memory_used_bytes, 1 / GIB),
      },
      {
        key: "sessions",
        title: t("HAProxy sessions"),
        unit: t("current"),
        points: m.haproxy_sessions,
        now: s.haproxy_sessions,
      },
      {
        key: "traffic",
        title: t("Traffic in"),
        unit: t("Mbit/s"),
        points: scaled(m.traffic_in_bps, 1e-6),
        now: num(s.traffic_in_bps, 1e-6),
      },
    ],
  };
}

export function EngineMetrics({ tenant, engineId }: { tenant: string; engineId: string }) {
  return (
    <MetricsView
      tenant={tenant}
      queryKey={["t", tenant, "metrics", "engine", engineId]}
      fetcher={async (windowS) =>
        unwrap(
          await api.GET("/api/v1/tenants/{t}/engines/{engine}/metrics", {
            params: { path: { t: tenant, engine: engineId }, query: { window_s: windowS } },
          }),
        )
      }
      toPanels={enginePanels}
      scopeLine={t("this engine only")}
    />
  );
}
