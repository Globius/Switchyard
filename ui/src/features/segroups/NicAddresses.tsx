// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * "Network addresses" section of the deploy sheet: one row per VIP or backend network of the SE
 * group, each with a pool picker. Every new engine gets one address on that network from the
 * chosen pool (`nic_pools` on the deploy request); an empty row leaves the NIC unaddressed.
 */
import { useQueries } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { AlertTriangle } from "lucide-react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { Combobox } from "@/components/ui/combobox";
import { t } from "@/lib/i18n";

type Pool = components["schemas"]["VipPoolRead"];
type Network = components["schemas"]["NetworkRead"];
type GroupNetwork = components["schemas"]["SeGroupNetwork"];

/** A row's pool: the user's choice, else the network's only pool, else none (empty string). */
export function effectivePool(
  choice: string | undefined,
  poolsOnNetwork: { id: string }[],
): string {
  if (choice !== undefined) return choice;
  return poolsOnNetwork.length === 1 ? (poolsOnNetwork[0]?.id ?? "") : "";
}

/** The `nic_pools` body: only rows that have a pool; undefined when none do. */
export function nicPoolsBody(
  rows: { network_id: string }[],
  choices: Record<string, string | undefined>,
  pools: Pool[],
): Record<string, string> | undefined {
  const out: Record<string, string> = {};
  for (const r of rows) {
    const on = pools.filter((p) => p.network_id === r.network_id);
    const pool = effectivePool(choices[r.network_id], on);
    if (pool) out[r.network_id] = pool;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

export function NicAddresses({
  tenant,
  networks,
  groupNetworks,
  pools,
  choices,
  onChoice,
  errors,
}: {
  tenant: string;
  networks: Network[];
  groupNetworks: GroupNetwork[];
  pools: Pool[];
  choices: Record<string, string | undefined>;
  onChoice: (networkId: string, pool: string) => void;
  /** Field errors keyed by network id (from `nic_pools.<network>` paths). */
  errors: Record<string, string>;
}) {
  const rows = groupNetworks.filter((n) => n.role !== "mgmt");
  const free = useQueries({
    queries: pools.map((p) => ({
      queryKey: ["t", tenant, "vip-pools", p.id, "allocations", { changeSet: undefined }],
      queryFn: async () =>
        unwrap(
          await api.GET("/api/v1/tenants/{t}/vip-pools/{pool_id}/allocations", {
            params: { path: { t: tenant, pool_id: p.id } },
          }),
        ),
    })),
  });
  const freeOf = new Map(pools.map((p, i) => [p.id, free[i]?.data?.free]));
  if (rows.length === 0) return null;
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-0.5 text-sm font-medium">{t("Network addresses")}</legend>
      <span className="-mt-1 text-xs text-muted-foreground">
        {t(
          "Each new engine gets one address on that network, from the pool you pick. Leave a row empty to configure that NIC yourself.",
        )}
      </span>
      <div className="rounded-md border">
        {rows.map((r, i) => {
          const net = networks.find((n) => n.id === r.network_id);
          const name = net?.name ?? r.network_id;
          const on = pools.filter((p) => p.network_id === r.network_id);
          const value = effectivePool(choices[r.network_id], on);
          const vip = r.role === "vip";
          return (
            <div key={r.network_id} className={i > 0 ? "border-t" : undefined}>
              <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] items-center gap-3 px-3 py-2.5">
                <span className="flex flex-col">
                  <span>
                    <span className="text-sm font-medium">{name}</span>{" "}
                    <span className="rounded-full bg-muted px-2 py-0.5 text-xs">
                      {vip ? t("VIP") : t("Backend")}
                    </span>
                  </span>
                  <span className="font-mono text-xs break-all text-muted-foreground">
                    {net?.cidr ?? ""}
                  </span>
                </span>
                <span className="flex flex-col gap-0.5">
                  <Combobox
                    aria-label={t("Addresses on {network}", { network: name })}
                    disabled={on.length === 0}
                    value={value}
                    onChange={(v) => onChoice(r.network_id, v)}
                    options={[
                      { value: "", label: t("Not addressed (set up by hand)") },
                      ...on.map((p) => {
                        const f = freeOf.get(p.id);
                        return {
                          value: p.id,
                          label:
                            f == null ? p.name : t("{pool} · {n} free", { pool: p.name, n: f }),
                        };
                      }),
                    ]}
                  />
                  {on.length === 0 && (
                    <span className="text-xs text-muted-foreground">
                      {t("No pools on this network.")}{" "}
                      <Link
                        to="/t/$tenant/networks"
                        params={{ tenant }}
                        className="text-primary hover:underline"
                      >
                        {t("Add one in Networks")}
                      </Link>
                    </span>
                  )}
                </span>
              </div>
              {errors[r.network_id] && (
                <p role="alert" className="px-3 pb-2 text-xs text-status-down">
                  {errors[r.network_id]}
                </p>
              )}
              {value === "" && (
                <p className="flex items-start gap-1.5 px-3 pb-2.5 text-xs text-status-degraded">
                  <AlertTriangle className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                  <span>
                    {vip
                      ? t(
                          "New engines can't serve VIPs on {network} until someone configures this NIC by hand.",
                          { network: name },
                        )
                      : t(
                          "New engines can't reach pool members on {network} until someone configures this NIC by hand.",
                          { network: name },
                        )}
                  </span>
                </p>
              )}
            </div>
          );
        })}
      </div>
      <span className="text-xs text-muted-foreground">
        {t(
          "Each picker lists only the pools on that network, with their free addresses. These are the same pools VIPs come from, so each engine uses one of their addresses.",
        )}
      </span>
    </fieldset>
  );
}
