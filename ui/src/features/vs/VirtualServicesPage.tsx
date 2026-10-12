import { Link, useNavigate } from "@tanstack/react-router";
import { Combobox } from "@/components/ui/combobox";
import { Plus } from "lucide-react";
import { useMemo } from "react";
import { useCan } from "@/auth/session";
import {
  DraftMarker,
  expiryStatus,
  ExpiryPill,
  HealthPill,
  StatusPill,
} from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton, UnavailableState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { Switch } from "@/components/ui/switch";
import { useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { useSaveOp } from "@/features/objects/save";
import { useChangeSetRead } from "@/features/review/queries";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import {
  useCertificates,
  useSeGroups,
  useVirtualServices,
  useVsMetrics,
  useVsStatus,
  type VsFilters,
} from "./queries";

const SELECT =
  "h-9 rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

/** Format a nullable metric; missing numbers show an em dash, never 0. */
export function metric(v: number | null | undefined, digits = 0, suffix = ""): string {
  return v == null ? "—" : `${v.toFixed(digits)}${suffix}`;
}

/** Virtual services list: filters in the URL, draft overlay, live health, metrics when available. */
export function VirtualServicesPage({ tenant, filters }: { tenant: string; filters: VsFilters }) {
  const navigate = useNavigate();
  const can = useCan(tenant);
  const create = can("write", "application");
  const { draft } = useDraft(tenant);
  const list = useVirtualServices(tenant, filters, draft?.id);
  const save = useSaveOp(tenant, "virtual_service");
  const draftSet = useChangeSetRead(tenant, draft?.id ?? "", !!draft);
  const qc = useQueryClient();
  /** Take a service back out of a deletion by removing the draft's delete op. */
  async function restore(id: string) {
    const index =
      draftSet.data?.ops.findIndex((o) => o.object_id === id && o.action === "delete") ?? -1;
    if (!draft || index < 0) return;
    unwrap(
      await api.DELETE("/api/v1/tenants/{t}/change-sets/{cs}/ops/{index}", {
        params: { path: { t: tenant, cs: draft.id, index } },
      }),
    );
    await qc.invalidateQueries({ queryKey: ["t", tenant] });
  }
  const status = useVsStatus(tenant);
  const metrics = useVsMetrics(tenant);
  const groups = useSeGroups(tenant);
  const certs = useCertificates(tenant, draft?.id);

  const rows = useMemo(() => list.data?.pages.flatMap((p) => p.items) ?? [], [list.data]);
  const groupName = new Map(groups.data?.items.map((g) => [g.id, g.name]));
  const statusById = new Map(status.data?.map((s) => [s.id, s]));
  const metricsById = new Map(metrics.data?.items.map((m) => [m.virtual_service_id, m.summary]));
  const certById = new Map(certs.data?.items.map((c) => [c.id, c]));
  const setFilter = (patch: Partial<VsFilters>) =>
    void navigate({
      to: "/t/$tenant/virtual-services",
      params: { tenant },
      search: (prev: VsFilters) => ({ ...prev, ...patch }),
    });
  const filtered = Object.values(filters).some((v) => v !== undefined && v !== "");

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("Virtual services")}</h1>
          <p className="text-sm text-muted-foreground">
            {draft
              ? t("{n} in {tenant}, shown as they will be after your draft", {
                  n: rows.length,
                  tenant,
                })
              : t("{n} in {tenant}", { n: rows.length, tenant })}
          </p>
        </div>
        <Tip label={create.reason}>
          <Button asChild={create.allowed} disabled={!create.allowed}>
            {create.allowed ? (
              <Link to="/t/$tenant/new" params={{ tenant }}>
                <Plus /> {t("New virtual service")}
              </Link>
            ) : (
              <>
                <Plus /> {t("New virtual service")}
              </>
            )}
          </Button>
        </Tip>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <input
          aria-label={t("Filter by name")}
          placeholder={t("Filter by name")}
          defaultValue={filters.q ?? ""}
          onChange={(e) => setFilter({ q: e.target.value || undefined })}
          className={cn(SELECT, "w-64")}
        />
        <Combobox
          aria-label={t("SE group")}
          className="w-48"
          placeholder={t("All SE groups")}
          value={filters.se_group ?? ""}
          onChange={(v) => setFilter({ se_group: v || undefined })}
          options={[
            { value: "", label: t("All SE groups") },
            ...(groups.data?.items ?? []).map((g) => ({ value: g.id, label: g.name })),
          ]}
        />
        <select
          aria-label={t("Protocol")}
          className={SELECT}
          value={filters.protocol ?? ""}
          onChange={(e) =>
            setFilter({ protocol: (e.target.value || undefined) as VsFilters["protocol"] })
          }
        >
          <option value="">{t("Any protocol")}</option>
          <option value="http">HTTP</option>
          <option value="tcp">TCP</option>
          <option value="udp">UDP</option>
        </select>
        <select
          aria-label={t("Enabled")}
          className={SELECT}
          value={filters.enabled === undefined ? "" : String(filters.enabled)}
          onChange={(e) =>
            setFilter({ enabled: e.target.value === "" ? undefined : e.target.value === "true" })
          }
        >
          <option value="">{t("Enabled or not")}</option>
          <option value="true">{t("Enabled")}</option>
          <option value="false">{t("Disabled")}</option>
        </select>
        <Tip label={t("Labels aren't supported yet")}>
          <Button variant="outline" size="sm" disabled>
            {t("Label")}
          </Button>
        </Tip>
      </div>

      <p className="text-xs text-muted-foreground">
        {t("Filters and sort are kept in the URL, so a view can be bookmarked or shared.")}
      </p>

      <div className="overflow-x-auto rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={9} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : rows.length === 0 ? (
          filtered ? (
            <EmptyState
              title={t("No virtual services match")}
              body={t("Clear a filter to see more.")}
            />
          ) : (
            <EmptyState
              title={t("No virtual services yet")}
              body={t("A virtual service is a VIP and ports that send traffic to your pools.")}
              action={
                create.allowed ? (
                  <Button asChild>
                    <Link to="/t/$tenant/new" params={{ tenant }}>
                      <Plus /> {t("Create a virtual service")}
                    </Link>
                  </Button>
                ) : undefined
              }
            />
          )
        ) : (
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
                <th className="px-4 py-2.5 font-medium">
                  <button
                    type="button"
                    onClick={() => setFilter({ sort: filters.sort === "name" ? "-name" : "name" })}
                    aria-label={t("Sort by name")}
                  >
                    {t("Name")} {filters.sort === "-name" ? "↓" : "↑"}
                  </button>
                </th>
                {[
                  "VIP · ports",
                  "Protocol",
                  "SE group",
                  "Health",
                  "RPS",
                  "p95",
                  "5xx",
                  "Cert expiry",
                  "Enabled",
                ].map((h) => (
                  <th key={h} className="px-4 py-2.5 font-medium">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((v) => {
                const s = statusById.get(v.id);
                const m = metricsById.get(v.id);
                const deleted = v.draft_status === "deleted";
                const expiry = v.certificate_ids
                  .map((id) => certById.get(id))
                  .filter((c) => c !== undefined)
                  .map((c) => expiryStatus(c.not_after))
                  .sort((a, b) => a.days - b.days)[0];
                return (
                  <tr key={v.id} className={deleted ? "text-muted-foreground" : undefined}>
                    <td className="px-4 py-3">
                      <span className="flex items-center gap-2">
                        {v.draft_status !== "unchanged" && <DraftMarker status={v.draft_status} />}
                        <Link
                          to="/t/$tenant/virtual-services/$id"
                          params={{ tenant, id: v.id }}
                          className={cn("font-medium text-primary", deleted && "line-through")}
                        >
                          {v.name}
                        </Link>
                      </span>
                    </td>
                    <td className="px-4 py-3 font-mono">
                      {v.vip ?? (
                        <span className="text-muted-foreground">{t("allocated on apply")}</span>
                      )}{" "}
                      · {v.ports.map((p) => p.port).join(", ")}
                    </td>
                    <td className="px-4 py-3 uppercase">{v.protocol}</td>
                    <td className="px-4 py-3">{groupName.get(v.se_group_id) ?? "—"}</td>
                    <td className="px-4 py-3">
                      {v.draft_status === "created" && !s ? (
                        <StatusPill status="neutral">{t("Not applied")}</StatusPill>
                      ) : deleted ? (
                        "—"
                      ) : !v.enabled ? (
                        <HealthPill health="disabled" />
                      ) : (
                        <HealthPill health={s?.health} />
                      )}
                    </td>
                    <td className="px-4 py-3">{metric(m?.rps, 1)}</td>
                    <td className="px-4 py-3">{metric(m?.p95_ms, 0, " ms")}</td>
                    <td className="px-4 py-3">
                      {m?.error_5xx_ratio == null
                        ? "—"
                        : `${(m.error_5xx_ratio * 100).toFixed(2)}%`}
                    </td>
                    <td className="px-4 py-3">
                      {v.certificate_ids.length === 0 ? (
                        <span className="text-muted-foreground">{t("No TLS")}</span>
                      ) : expiry ? (
                        <ExpiryPill ex={expiry} />
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-4 py-3">
                      {deleted ? (
                        <Tip label={create.reason}>
                          <Button
                            variant="ghost"
                            size="sm"
                            disabled={!create.allowed}
                            onClick={() => void restore(v.id)}
                          >
                            {t("Restore")}
                          </Button>
                        </Tip>
                      ) : (
                        <Tip label={create.reason}>
                          <Switch
                            aria-label={t("Enable {name}", { name: v.name })}
                            checked={v.enabled}
                            disabled={!create.allowed}
                            onCheckedChange={(on) =>
                              void save.mutateAsync({
                                action: "update",
                                id: v.id,
                                version: v.version,
                                body: { enabled: on },
                              })
                            }
                          />
                        </Tip>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {list.hasNextPage && (
        <Button
          variant="outline"
          disabled={list.isFetchingNextPage}
          onClick={() => void list.fetchNextPage()}
        >
          {t("Load more")}
        </Button>
      )}

      <div className="space-y-1">
        <p className="text-xs text-muted-foreground">
          {t("Health and member counts from engine status.")}
        </p>
        <p className="text-xs text-muted-foreground">{t("Sort by name · pages of 50")}</p>
        {metrics.data && !metrics.data.available && (
          <UnavailableState
            title={t("Metrics unavailable")}
            reason={t("{reason}, so RPS, p95 and 5xx show —", {
              reason: metrics.data.reason ?? t("no metrics backend is configured"),
            })}
          />
        )}
      </div>
    </div>
  );
}
