// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Networks and VIP pools: the subnets engines attach to, the ranges VIPs come from, and who holds each address. */
import { compareIp } from "@/lib/ip";
import { Combobox } from "@/components/ui/combobox";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { DraftMarker, StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Tip } from "@/components/ui/tooltip";
import { useSaveOp } from "@/features/objects/save";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Network = components["schemas"]["NetworkRead"];
type Pool = components["schemas"]["VipPoolRead"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

function useNetworks(tenant: string, changeSet?: string) {
  return useQuery({
    queryKey: ["t", tenant, "networks", { changeSet }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/networks", {
          params: { path: { t: tenant }, query: { limit: 200, change_set: changeSet } },
        }),
      ),
  });
}

function usePools(tenant: string, changeSet?: string) {
  return useQuery({
    queryKey: ["t", tenant, "vip-pools", { changeSet }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/vip-pools", {
          params: { path: { t: tenant }, query: { limit: 200, change_set: changeSet } },
        }),
      ),
  });
}

function useSdnStatus(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "sdn-status"],
    refetchInterval: 8000,
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/sdn/status", { params: { path: { t: tenant } } })),
  });
}

/** Chip for a network's latest SDN apply: queued or running, or the failure. Nothing when idle. */
export function SdnChip({ state, error }: { state: string; error?: string | null }) {
  if (state === "queued" || state === "running")
    return <StatusPill status="pending">{t("SDN apply running")}</StatusPill>;
  if (state === "failed")
    return (
      <span title={error ?? undefined}>
        <StatusPill status="down">{t("SDN apply failed")}</StatusPill>
      </span>
    );
  return null;
}

function useGroups(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "se-groups"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/se-groups", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
}

function useUsage(tenant: string, pool: string, changeSet?: string) {
  return useQuery({
    queryKey: ["t", tenant, "vip-pools", pool, "allocations", { changeSet }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/vip-pools/{pool_id}/allocations", {
          params: { path: { t: tenant, pool_id: pool }, query: { change_set: changeSet } },
        }),
      ),
  });
}

/** `10.40.12.10 – 10.40.12.71` for each range of a pool. */
export function rangesText(p: Pick<Pool, "ranges">): string {
  return p.ranges.map((r) => `${r.start} – ${r.end}`).join(", ");
}

function Field({
  label,
  error,
  hint,
  children,
}: {
  label: string;
  error?: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block space-y-1.5 text-sm font-medium">
      {label}
      {children}
      {hint && <span className="block text-xs font-normal text-muted-foreground">{hint}</span>}
      {error && (
        <span role="alert" className="block text-xs font-normal text-status-down">
          {error}
        </span>
      )}
    </label>
  );
}

function errorsOf(e: unknown): Record<string, string> | null {
  if (!(e instanceof ApiError) || e.status !== 422) return null;
  const m: Record<string, string> = {};
  for (const x of e.problem?.errors ?? []) m[x.path.split(".")[0] ?? x.path] = x.message;
  return m;
}

function NetworkSheet({
  tenant,
  network,
  mode,
  onClose,
}: {
  tenant: string;
  network: Network | null;
  mode: "create" | "attach";
  onClose: () => void;
}) {
  const save = useSaveOp(tenant, "network");
  const can = useCan(tenant);
  const write = can("write", "application");
  const base = useMemo(
    () => ({
      name: network?.name ?? "",
      cidr: network?.cidr ?? "",
      gateway: network?.gateway ?? "",
      sdn_zone: network?.sdn_zone ?? "",
      vnet: network?.vnet ?? "",
      vlan_or_vni: network?.vlan_or_vni != null ? String(network.vlan_or_vni) : "",
    }),
    [network],
  );
  const [f, setF] = useState(base);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<unknown>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const managed = network ? network.managed : mode === "create";
  const body = (k: Partial<typeof f>) => {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(k) as (keyof typeof f)[]) {
      const v = k[key]!;
      out[key] = key === "vlan_or_vni" ? (v === "" ? null : Number(v)) : v === "" ? null : v;
    }
    return out;
  };
  const changed = (Object.keys(f) as (keyof typeof f)[]).filter((k) => f[k] !== base[k]);
  const dirty = network ? changed.length > 0 : f.name !== "" && f.cidr !== "";
  const input = (k: keyof typeof f, label: string, mono = false, hint?: string) => (
    <Field label={label} error={errors[k === "vlan_or_vni" ? "vlan_or_vni" : k]} hint={hint}>
      <input
        aria-label={label}
        className={cn(INPUT, mono && "font-mono")}
        disabled={!write.allowed}
        value={f[k]}
        onChange={(e) => setF({ ...f, [k]: e.target.value })}
      />
    </Field>
  );
  async function submit() {
    setErrors({});
    setProblem(null);
    try {
      if (network)
        await save.mutateAsync({
          action: "update",
          id: network.id,
          version: network.version,
          body: body(Object.fromEntries(changed.map((k) => [k, f[k]]))),
        });
      else
        await save.mutateAsync({ action: "create", body: { ...body(f), name: f.name, managed } });
      onClose();
    } catch (e) {
      const m = errorsOf(e);
      if (m) setErrors(m);
      else setProblem(e);
    }
  }
  return (
    <>
      <Sheet
        open
        onOpenChange={(o) => !o && onClose()}
        title={
          network
            ? t("Edit network {name}", { name: network.name })
            : mode === "create"
              ? t("Create network")
              : t("Attach existing network")
        }
        badge={
          network && network.draft_status !== "unchanged" ? (
            <DraftMarker status={network.draft_status} />
          ) : undefined
        }
        footer={
          <>
            {network && (
              <Tip label={write.reason}>
                <Button
                  variant="outline"
                  disabled={!write.allowed}
                  onClick={() => setConfirmDelete(true)}
                >
                  {t("Delete")}
                </Button>
              </Tip>
            )}
            <Button variant="ghost" className="ml-auto" onClick={onClose}>
              {t("Cancel")}
            </Button>
            <Tip label={write.reason}>
              <Button
                disabled={!write.allowed || !dirty || save.isPending}
                onClick={() => void submit()}
              >
                {t("Save to draft")}
              </Button>
            </Tip>
          </>
        }
      >
        {problem != null && <ErrorState error={problem} />}
        <p className="text-sm text-muted-foreground">
          {managed
            ? t(
                "Switchyard creates this network in Proxmox SDN when the change set is applied (Apply SDN).",
              )
            : t("An existing network: Switchyard only uses it and never changes it in Proxmox.")}
        </p>
        {input("name", t("Name"))}
        <div className="grid grid-cols-2 gap-3">
          {input(
            "cidr",
            t("CIDR"),
            true,
            t("IPv4 or IPv6, for example 10.40.12.0/24 or 2001:db8:12::/64"),
          )}
          {input("gateway", t("Gateway"), true, t("In the same family as the CIDR"))}
          {input("sdn_zone", t("SDN zone"))}
          {input("vnet", t("VNet"))}
          {input("vlan_or_vni", t("VLAN / VNI"), false, t("A VLAN id, or a VNI for EVPN."))}
        </div>
      </Sheet>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete network {name}?", { name: network?.name ?? "" })}
        confirmLabel={t("Add delete to draft")}
        busy={save.isPending}
        onConfirm={async () => {
          if (!network) return;
          try {
            await save.mutateAsync({ action: "delete", id: network.id, version: network.version });
            onClose();
          } catch (e) {
            setProblem(e);
          } finally {
            setConfirmDelete(false);
          }
        }}
      >
        <p>
          {t(
            "Blocked by the validator while an SE group or VIP pool uses it. Nothing changes until the change set is applied.",
          )}
        </p>
      </ConfirmDialog>
    </>
  );
}

function PoolSheet({
  tenant,
  pool,
  networks,
  onClose,
}: {
  tenant: string;
  pool: Pool | "new";
  networks: Network[];
  onClose: () => void;
}) {
  const save = useSaveOp(tenant, "vip_pool");
  const can = useCan(tenant);
  const write = can("write", "application");
  const [name, setName] = useState(pool === "new" ? "" : pool.name);
  const [network, setNetwork] = useState(pool === "new" ? "" : pool.network_id);
  const [ranges, setRanges] = useState<{ start: string; end: string }[]>(
    pool === "new"
      ? [{ start: "", end: "" }]
      : pool.ranges.map((r) => ({ start: r.start, end: r.end })),
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<unknown>(null);
  async function submit() {
    setErrors({});
    setProblem(null);
    try {
      if (pool === "new")
        await save.mutateAsync({ action: "create", body: { name, network_id: network, ranges } });
      else
        await save.mutateAsync({
          action: "update",
          id: pool.id,
          version: pool.version,
          body: { name, network_id: network, ranges },
        });
      onClose();
    } catch (e) {
      const m = errorsOf(e);
      if (m) setErrors(m);
      else setProblem(e);
    }
  }
  const valid = name !== "" && network !== "" && ranges.every((r) => r.start && r.end);
  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={pool === "new" ? t("New VIP pool") : t("Edit ranges of {name}", { name: pool.name })}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Tip label={write.reason}>
            <Button
              disabled={!write.allowed || !valid || save.isPending}
              onClick={() => void submit()}
            >
              {t("Save to draft")}
            </Button>
          </Tip>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      <Field label={t("Name")} error={errors.name}>
        <input
          aria-label={t("Name")}
          className={INPUT}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </Field>
      <Field label={t("Network")} error={errors.network_id}>
        <Combobox
          aria-label={t("Network")}
          value={network}
          onChange={setNetwork}
          options={networks.map((n) => ({ value: n.id, label: `${n.name} · ${n.cidr}` }))}
        />
      </Field>
      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium">{t("Ranges")}</h3>
          <Button
            size="sm"
            variant="outline"
            onClick={() => setRanges([...ranges, { start: "", end: "" }])}
          >
            {t("Add range")}
          </Button>
        </div>
        {errors.ranges && (
          <p role="alert" className="text-xs text-status-down">
            {errors.ranges}
          </p>
        )}
        {ranges.map((r, i) => (
          <div key={i} className="flex items-center gap-2">
            <input
              aria-label={t("Range {n} start", { n: i + 1 })}
              className={cn(INPUT, "font-mono")}
              value={r.start}
              onChange={(e) =>
                setRanges(ranges.map((x, j) => (j === i ? { ...x, start: e.target.value } : x)))
              }
            />
            <span>–</span>
            <input
              aria-label={t("Range {n} end", { n: i + 1 })}
              className={cn(INPUT, "font-mono")}
              value={r.end}
              onChange={(e) =>
                setRanges(ranges.map((x, j) => (j === i ? { ...x, end: e.target.value } : x)))
              }
            />
            <Button
              size="sm"
              variant="ghost"
              aria-label={t("Remove range {n}", { n: i + 1 })}
              disabled={ranges.length === 1}
              onClick={() => setRanges(ranges.filter((_, j) => j !== i))}
            >
              ×
            </Button>
          </div>
        ))}
      </section>
    </Sheet>
  );
}

function PoolRow({
  tenant,
  pool,
  network,
  draftId,
  selected,
  onSelect,
  onEdit,
}: {
  tenant: string;
  pool: Pool;
  network: Network | undefined;
  draftId?: string;
  selected: boolean;
  onSelect: () => void;
  onEdit: () => void;
}) {
  const live = useUsage(tenant, pool.id);
  const after = useUsage(tenant, pool.id, draftId);
  const used = live.data?.used ?? 0;
  const total = live.data?.total ?? 0;
  const afterUsed = after.data?.used;
  return (
    <tr className={cn(selected && "bg-accent/40")}>
      <td className="px-4 py-3">
        <button
          type="button"
          className="font-medium text-primary hover:underline"
          onClick={onSelect}
        >
          {pool.name}
        </button>
        {pool.draft_status !== "unchanged" && (
          <span className="ml-2 inline-flex items-center gap-1">
            <DraftMarker status={pool.draft_status} />
          </span>
        )}
      </td>
      <td className="px-4 py-3">{network?.name ?? "—"}</td>
      <td className="px-4 py-3 font-mono text-xs">{rangesText(pool)}</td>
      <td className="px-4 py-3">
        <div className="text-xs">
          {t("{used} of {total} used", { used, total })}
          {afterUsed !== undefined && afterUsed !== used && (
            <span className="text-muted-foreground">
              {" · "}
              {t("{n} after draft", { n: afterUsed })}
            </span>
          )}
        </div>
        <div className="mt-1 h-1.5 w-48 overflow-hidden rounded-full bg-muted" aria-hidden>
          <div
            className="h-full bg-primary"
            style={{ width: `${total ? Math.min(100, (used / total) * 100) : 0}%` }}
          />
        </div>
      </td>
      <td className="px-4 py-3 text-right">
        <Button size="sm" variant="outline" onClick={onEdit}>
          {t("Edit ranges")}
        </Button>
      </td>
    </tr>
  );
}

function Addresses({ tenant, pool, draftId }: { tenant: string; pool: Pool; draftId?: string }) {
  const live = useUsage(tenant, pool.id);
  const after = useUsage(tenant, pool.id, draftId);
  const [q, setQ] = useState("");
  const rows = useMemo(() => {
    const byAddr = new Map<
      string,
      { address: string; name: string; id: string; enabled: boolean; state: string }
    >();
    for (const a of live.data?.allocations ?? [])
      byAddr.set(a.address, {
        address: a.address,
        name: a.virtual_service_name,
        id: a.virtual_service_id,
        enabled: a.enabled,
        state: "live",
      });
    const afterAddrs = new Set((after.data?.allocations ?? []).map((a) => a.address));
    if (draftId) {
      for (const [addr, r] of byAddr) if (!afterAddrs.has(addr)) r.state = "freed";
      for (const a of after.data?.allocations ?? [])
        if (!byAddr.has(a.address))
          byAddr.set(a.address, {
            address: a.address,
            name: a.virtual_service_name,
            id: a.virtual_service_id,
            enabled: a.enabled,
            state: "draft",
          });
    }
    return [...byAddr.values()].sort((x, y) => compareIp(x.address, y.address));
  }, [live.data, after.data, draftId]);
  const shown = rows.filter((r) =>
    `${r.address} ${r.name}`.toLowerCase().includes(q.trim().toLowerCase()),
  );
  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <header className="flex items-center gap-3 border-b px-4 py-3">
        <h2 className="text-base font-semibold">{t("Addresses in {name}", { name: pool.name })}</h2>
        <input
          aria-label={t("Find IP or service")}
          placeholder={t("Find IP or service")}
          className={cn(INPUT, "ml-auto max-w-56")}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
      </header>
      {live.isPending ? (
        <TableSkeleton cols={3} />
      ) : live.isError ? (
        <ErrorState error={live.error} onRetry={() => void live.refetch()} />
      ) : shown.length === 0 ? (
        <p className="p-4 text-sm text-muted-foreground">{t("No addresses in use.")}</p>
      ) : (
        <table className="w-full text-[13px]">
          <thead className="text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-4 py-2 font-medium">{t("Address")}</th>
              <th className="px-4 py-2 font-medium">{t("Used by")}</th>
              <th className="px-4 py-2 font-medium">{t("State")}</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody className="divide-y">
            {shown.map((r) => (
              <tr key={r.address} className={cn(r.state !== "live" && "bg-draft/10")}>
                <td className="px-4 py-2.5 font-mono text-xs">{r.address}</td>
                <td className="px-4 py-2.5">
                  <Link
                    to="/t/$tenant/virtual-services/$id"
                    params={{ tenant, id: r.id }}
                    className={cn(
                      "text-primary hover:underline",
                      r.state === "freed" && "line-through",
                    )}
                  >
                    {r.name}
                  </Link>
                  {!r.enabled && (
                    <span className="ml-2 text-xs text-muted-foreground">{t("disabled")}</span>
                  )}
                </td>
                <td className="px-4 py-2.5">
                  {r.state === "freed" ? (
                    <StatusPill status="draft">{t("Freed by draft")}</StatusPill>
                  ) : r.state === "draft" ? (
                    <StatusPill status="draft">{t("In a draft · not reserved")}</StatusPill>
                  ) : (
                    <StatusPill status="neutral">
                      {r.enabled ? t("Live") : t("Live · held")}
                    </StatusPill>
                  )}
                </td>
                <td className="px-4 py-2 text-right">
                  {r.state === "live" && !r.enabled && (
                    <Link
                      to="/t/$tenant/virtual-services/$id/settings"
                      params={{ tenant, id: r.id }}
                      className="inline-flex h-8 items-center rounded-md border px-3 text-sm hover:bg-accent"
                    >
                      {t("Edit service")}
                    </Link>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="border-t px-4 py-3 text-xs text-muted-foreground">
        {t(
          "An address is held by the virtual service that uses it; there is no separate reservation. To free it, edit the service's VIP or delete the service in a draft; it goes through review like any change.",
        )}
      </p>
    </section>
  );
}

export function Networks({ tenant }: { tenant: string }) {
  const { draft } = useDraft(tenant);
  const networks = useNetworks(tenant, draft?.id);
  const pools = usePools(tenant, draft?.id);
  const groups = useGroups(tenant);
  const sdnStatus = useSdnStatus(tenant);
  const can = useCan(tenant);
  const write = can("write", "application");
  const manage = can("apply", "sdn");
  const qc = useQueryClient();
  const [sheet, setSheet] = useState<
    | { kind: "network"; network: Network | null; mode: "create" | "attach" }
    | { kind: "pool"; pool: Pool | "new" }
    | null
  >(null);
  const [picked, setPicked] = useState<string | null>(null);
  const sdn = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/networks/apply-sdn", {
          params: { path: { t: tenant } },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["t", tenant] }),
  });
  const nets = networks.data?.items ?? [];
  const poolItems = pools.data?.items ?? [];
  const pool = poolItems.find((p) => p.id === picked) ?? poolItems[0];
  const usedBy = (n: Network) => [
    ...(groups.data?.items ?? [])
      .filter((g) => g.networks.some((x) => x.network_id === n.id))
      .map((g) => {
        const role = g.networks.find((x) => x.network_id === n.id)!.role;
        return `${g.name} (${{ vip: "VIP", backend: "backend", mgmt: "mgmt" }[role]})`;
      }),
    ...poolItems.filter((p) => p.network_id === n.id).map((p) => p.name),
  ];
  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("Networks and VIP pools")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("Subnets the SEs attach to, and the ranges VIPs are allocated from")}
          </p>
        </div>
        <div className="flex gap-2">
          <Tip label={write.reason}>
            <Button
              variant="outline"
              disabled={!write.allowed}
              onClick={() => setSheet({ kind: "network", network: null, mode: "attach" })}
            >
              {t("Attach existing network")}
            </Button>
          </Tip>
          <Tip label={write.reason}>
            <Button
              disabled={!write.allowed}
              onClick={() => setSheet({ kind: "network", network: null, mode: "create" })}
            >
              {t("Create network")}
            </Button>
          </Tip>
        </div>
      </div>

      <section className="overflow-hidden rounded-lg border bg-card">
        <header className="flex items-center gap-2 border-b px-4 py-3">
          <h2 className="text-base font-semibold">{t("Networks")}</h2>
          <span className="text-xs text-muted-foreground">{nets.length}</span>
          <Tip label={manage.reason}>
            <Button
              size="sm"
              variant="outline"
              className="ml-auto"
              disabled={!manage.allowed || sdn.isPending}
              onClick={() => sdn.mutate()}
            >
              {t("Apply SDN")}
            </Button>
          </Tip>
        </header>
        {sdn.error && <ErrorState error={sdn.error} />}
        {sdn.isSuccess && (
          <p role="status" className="border-b px-4 py-2 text-sm">
            {t("SDN apply started as a job. Follow it on the Jobs screen.")}
          </p>
        )}
        {networks.isPending ? (
          <TableSkeleton cols={7} />
        ) : networks.isError ? (
          <ErrorState error={networks.error} onRetry={() => void networks.refetch()} />
        ) : nets.length === 0 ? (
          <EmptyState
            title={t("No networks yet")}
            body={t("Create a network in Proxmox SDN, or attach one that already exists.")}
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[13px]">
              <thead className="text-left text-xs text-muted-foreground">
                <tr>
                  {[
                    "Name",
                    "CIDR",
                    "Gateway",
                    "SDN zone · VNet",
                    "VLAN / VNI",
                    "Ownership",
                    "Used by",
                  ].map((h) => (
                    <th key={h} className="px-4 py-2.5 font-medium">
                      {t(h)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {nets.map((n) => (
                  <tr key={n.id}>
                    <td className="px-4 py-3">
                      <span className="flex items-center gap-2">
                        {n.draft_status !== "unchanged" && <DraftMarker status={n.draft_status} />}
                        <button
                          type="button"
                          className={cn(
                            "font-medium text-primary hover:underline",
                            n.draft_status === "deleted" && "line-through",
                          )}
                          onClick={() => setSheet({ kind: "network", network: n, mode: "create" })}
                        >
                          {n.name}
                        </button>
                        {(() => {
                          const st = sdnStatus.data?.find((x) => x.network_id === n.id);
                          return st ? <SdnChip state={st.state} error={st.error} /> : null;
                        })()}
                      </span>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs break-all">{n.cidr}</td>
                    <td className="px-4 py-3 font-mono text-xs break-all">{n.gateway ?? "—"}</td>
                    <td className="px-4 py-3 font-mono text-xs">
                      {n.sdn_zone || n.vnet ? `${n.sdn_zone ?? "—"} · ${n.vnet ?? "—"}` : "—"}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs">
                      {n.vlan_or_vni != null
                        ? n.sdn_zone?.startsWith("evpn")
                          ? `VNI ${n.vlan_or_vni}`
                          : `VLAN ${n.vlan_or_vni}`
                        : "—"}
                    </td>
                    <td className="px-4 py-3">
                      <StatusPill status="neutral">
                        {n.managed ? t("Created by Switchyard") : t("Attached")}
                      </StatusPill>
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {usedBy(n).join(", ") || "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="overflow-hidden rounded-lg border bg-card">
        <header className="flex items-center gap-2 border-b px-4 py-3">
          <h2 className="text-base font-semibold">{t("VIP pools")}</h2>
          <span className="text-xs text-muted-foreground">{poolItems.length}</span>
          <Tip label={write.reason}>
            <Button
              size="sm"
              variant="outline"
              className="ml-auto"
              disabled={!write.allowed}
              onClick={() => setSheet({ kind: "pool", pool: "new" })}
            >
              {t("New VIP pool")}
            </Button>
          </Tip>
        </header>
        {pools.isPending ? (
          <TableSkeleton cols={5} />
        ) : pools.isError ? (
          <ErrorState error={pools.error} onRetry={() => void pools.refetch()} />
        ) : poolItems.length === 0 ? (
          <EmptyState
            title={t("No VIP pools yet")}
            body={t(
              "A pool is a set of address ranges inside a network; new VIPs are allocated from it.",
            )}
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="text-left text-xs text-muted-foreground">
              <tr>
                {["Name", "Network", "Ranges", "Usage", ""].map((h, i) => (
                  <th key={i} className="px-4 py-2.5 font-medium">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {poolItems.map((p) => (
                <PoolRow
                  key={p.id}
                  tenant={tenant}
                  pool={p}
                  network={nets.find((n) => n.id === p.network_id)}
                  draftId={draft?.id}
                  selected={pool?.id === p.id}
                  onSelect={() => setPicked(p.id)}
                  onEdit={() => setSheet({ kind: "pool", pool: p })}
                />
              ))}
            </tbody>
          </table>
        )}
      </section>

      {pool && <Addresses tenant={tenant} pool={pool} draftId={draft?.id} />}

      <p className="text-xs text-muted-foreground">
        {t(
          "Deleting a network is blocked while an SE group or VIP pool uses it. Deleting a VIP pool is blocked while any virtual service has an address from it.",
        )}
      </p>

      {sheet?.kind === "network" && (
        <NetworkSheet
          key={sheet.network ? `${sheet.network.id}-${sheet.network.version}` : sheet.mode}
          tenant={tenant}
          network={sheet.network}
          mode={sheet.mode}
          onClose={() => setSheet(null)}
        />
      )}
      {sheet?.kind === "pool" && (
        <PoolSheet
          key={sheet.pool === "new" ? "new" : `${sheet.pool.id}-${sheet.pool.version}`}
          tenant={tenant}
          pool={sheet.pool}
          networks={nets}
          onClose={() => setSheet(null)}
        />
      )}
    </div>
  );
}
