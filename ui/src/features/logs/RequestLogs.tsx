/**
 * Request logs: the tenant's HAProxy access log lines, newest first, from the controller's
 * tenant-scoped log API (never straight from the log store). Filter by service, status class,
 * time and text; "Load older" follows the cursor.
 */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton, UnavailableState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { shortWhen } from "@/lib/format";
import { t } from "@/lib/i18n";

const INPUT =
  "h-9 rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";
const RANGES: [string, string, number][] = [
  ["15m", "Last 15 minutes", 900],
  ["1h", "Last hour", 3600],
  ["24h", "Last 24 hours", 86400],
  ["7d", "Last 7 days", 604800],
];

/** Status class of an HTTP status for the pill. */
export function statusTone(status: number): "healthy" | "neutral" | "degraded" | "down" {
  if (status >= 500) return "down";
  if (status >= 400) return "degraded";
  if (status >= 200 && status < 300) return "healthy";
  return "neutral";
}

export function RequestLogs({ tenant }: { tenant: string }) {
  const [service, setService] = useState("");
  const [cls, setCls] = useState("");
  const [range, setRange] = useState("1h");
  const [q, setQ] = useState("");
  const [applied, setApplied] = useState("");
  const services = useQuery({
    queryKey: ["t", tenant, "virtual-services", "names"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/virtual-services", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
  const seconds = RANGES.find((r) => r[0] === range)?.[2] ?? 3600;
  const logs = useInfiniteQuery({
    queryKey: ["t", tenant, "logs", { service, cls, range, applied }],
    initialPageParam: undefined as string | undefined,
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/logs", {
          params: {
            path: { t: tenant },
            query: {
              service: service || undefined,
              status_class: cls ? (Number(cls) as 2) : undefined,
              q: applied || undefined,
              since: new Date(Date.now() - seconds * 1000).toISOString(),
              cursor: pageParam,
              limit: 100,
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const first = logs.data?.pages[0];
  const entries = logs.data?.pages.flatMap((p) => p.entries) ?? [];

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">{t("Request logs")}</h1>
        <p className="text-sm text-muted-foreground">
          {t("HTTP requests through {tenant}'s virtual services, newest first.", { tenant })}
        </p>
      </div>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          setApplied(q);
        }}
      >
        <div className="w-56">
          <Combobox
            aria-label={t("Virtual service")}
            value={service}
            onChange={setService}
            placeholder={t("All virtual services")}
            options={[
              { value: "", label: t("All virtual services") },
              ...(services.data?.items ?? []).map((v) => ({ value: v.id, label: v.name })),
            ]}
          />
        </div>
        <select
          aria-label={t("Status")}
          className={INPUT}
          value={cls}
          onChange={(e) => setCls(e.target.value)}
        >
          <option value="">{t("Any status")}</option>
          {["2", "3", "4", "5"].map((c) => (
            <option key={c} value={c}>
              {c}xx
            </option>
          ))}
        </select>
        <select
          aria-label={t("Time range")}
          className={INPUT}
          value={range}
          onChange={(e) => setRange(e.target.value)}
        >
          {RANGES.map(([k, label]) => (
            <option key={k} value={k}>
              {t(label)}
            </option>
          ))}
        </select>
        <input
          aria-label={t("Search")}
          placeholder={t("Search path, client or server")}
          className={`${INPUT} w-64`}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <Button type="submit" variant="outline">
          {t("Search")}
        </Button>
      </form>
      <div className="overflow-x-auto rounded-lg border bg-card">
        {logs.isPending ? (
          <TableSkeleton cols={6} />
        ) : logs.isError ? (
          <ErrorState error={logs.error} onRetry={() => void logs.refetch()} />
        ) : first && !first.available ? (
          <div className="p-4">
            <UnavailableState title={t("Logs unavailable")} reason={first.reason} />
          </div>
        ) : entries.length === 0 ? (
          <EmptyState
            title={t("No requests in this range")}
            body={t("Try a longer range or fewer filters.")}
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["Time", "Request", "Status", "Duration", "Client", "Served by"].map((h) => (
                  <th key={h} className="px-4 py-2.5 font-medium whitespace-nowrap">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {entries.map((e, i) => (
                <tr key={`${e.at}-${i}`}>
                  <td className="px-4 py-2 whitespace-nowrap text-muted-foreground">
                    {shortWhen(e.at)}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs break-all">
                    <span className="font-semibold">{e.method ?? "—"}</span> {e.path ?? ""}
                    <span className="block text-muted-foreground">{e.frontend}</span>
                  </td>
                  <td className="px-4 py-2">
                    <StatusPill status={statusTone(e.status)}>{e.status}</StatusPill>
                    {e.waf && (
                      <StatusPill status="degraded">
                        {e.waf_action === "drop" ? t("WAF dropped") : t("WAF denied")}
                      </StatusPill>
                    )}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs whitespace-nowrap">
                    {e.duration_ms} ms
                  </td>
                  <td className="px-4 py-2 font-mono text-xs break-all">{e.client_ip}</td>
                  <td className="px-4 py-2 font-mono text-xs break-all">
                    {e.server}
                    <span className="block text-muted-foreground">{e.backend}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {logs.hasNextPage && (
        <Button
          variant="outline"
          disabled={logs.isFetchingNextPage}
          onClick={() => void logs.fetchNextPage()}
        >
          {t("Load older")}
        </Button>
      )}
    </div>
  );
}
