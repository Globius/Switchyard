import { useQuery } from "@tanstack/react-query";
import { Combobox } from "@/components/ui/combobox";
import { useNavigate } from "@tanstack/react-router";
import { Info, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { DraftMarker, StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useSaveOp } from "@/features/objects/save";
import { useDraft } from "@/features/shell/draft";
import { usePools, type PoolRead } from "@/features/vs/detail-queries";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Member = PoolRead["members"][number];
export interface PoolForm {
  name: string;
  algorithm: PoolRead["algorithm"];
  health_monitor_id: string | null;
  tls: boolean;
  tls_verify: boolean;
  persistence: { type: "source_ip"; table_size: number; timeout_s: number } | null;
  passive_check: PassiveCheck | null;
  slow_start_s: number | null;
  members: Member[];
}
export type PassiveCheck = {
  observe: "layer4" | "layer7";
  error_limit: number;
  on_error: "mark-down" | "fastinter" | "fail-check" | "sudden-death";
};

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";
const ALGO: Record<string, string> = {
  roundrobin: "Round robin",
  leastconn: "Least connections",
  source: "Source hash",
  random: "Random",
};

export function emptyPool(): PoolForm {
  return {
    name: "",
    algorithm: "roundrobin",
    health_monitor_id: null,
    tls: false,
    tls_verify: false,
    persistence: null,
    passive_check: null,
    slow_start_s: null,
    members: [],
  };
}

export function toForm(p: PoolRead): PoolForm {
  return {
    name: p.name,
    algorithm: p.algorithm,
    health_monitor_id: p.health_monitor_id ?? null,
    tls: p.tls,
    tls_verify: p.tls_verify,
    persistence: p.persistence
      ? {
          type: "source_ip",
          table_size: p.persistence.table_size ?? 100000,
          timeout_s: p.persistence.timeout_s ?? 1800,
        }
      : null,
    passive_check: p.passive_check ? { ...p.passive_check } : null,
    slow_start_s: p.slow_start_s ?? null,
    members: structuredClone(p.members),
  };
}

/** Only the fields that differ, as an update op body. */
export function poolPatch(base: PoolForm, next: PoolForm): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of Object.keys(next) as (keyof PoolForm)[]) {
    if (JSON.stringify(base[k]) !== JSON.stringify(next[k])) out[k] = next[k];
  }
  return out;
}

export function persistenceLabel(p: PoolForm["persistence"]): string {
  return p ? `Source IP · ${Math.round(p.timeout_s / 60)} min` : "—";
}

function useMonitors(tenant: string, changeSet?: string) {
  return useQuery({
    queryKey: ["t", tenant, "health-monitors", { changeSet }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/health-monitors", {
          params: { path: { t: tenant }, query: { limit: 200, change_set: changeSet } },
        }),
      ),
  });
}

function useServices(tenant: string, changeSet?: string) {
  return useQuery({
    queryKey: ["t", tenant, "virtual-services", "all", { changeSet }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/virtual-services", {
          params: { path: { t: tenant }, query: { limit: 200, change_set: changeSet } },
        }),
      ),
  });
}

function PoolSheet({
  tenant,
  pool,
  onClose,
}: {
  tenant: string;
  pool: PoolRead | "new";
  onClose: () => void;
}) {
  const monitors = useMonitors(tenant);
  const save = useSaveOp(tenant, "pool");
  const can = useCan(tenant);
  const write = can("write", "application");
  const base = useMemo(() => (pool === "new" ? emptyPool() : toForm(pool)), [pool]);
  const [f, setF] = useState<PoolForm>(base);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<unknown>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const patch = poolPatch(base, f);
  const dirty = pool === "new" ? f.name !== "" : Object.keys(patch).length > 0;
  const set = (p: Partial<PoolForm>) => setF({ ...f, ...p });
  const setMember = (i: number, p: Partial<Member>) =>
    set({ members: f.members.map((m, j) => (j === i ? { ...m, ...p } : m)) });

  async function submit() {
    setProblem(null);
    setErrors({});
    try {
      if (pool === "new")
        await save.mutateAsync({ action: "create", body: { ...f, members: f.members } });
      else
        await save.mutateAsync({
          action: "update",
          id: pool.id,
          version: pool.version,
          body: patch,
        });
      onClose();
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        const m: Record<string, string> = {};
        for (const x of e.problem?.errors ?? []) m[x.path] = x.message;
        setErrors(m);
      } else setProblem(e);
    }
  }

  return (
    <>
      <Sheet
        open
        onOpenChange={(o) => !o && onClose()}
        title={pool === "new" ? t("New pool") : t("Edit pool {name}", { name: pool.name })}
        badge={
          pool !== "new" && pool.draft_status !== "unchanged" ? (
            <DraftMarker status={pool.draft_status} />
          ) : undefined
        }
        footer={
          <>
            {pool !== "new" && (
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
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Name")}
            <input
              aria-label={t("Name")}
              className={INPUT}
              disabled={!write.allowed}
              value={f.name}
              onChange={(e) => set({ name: e.target.value })}
            />
            {errors.name && (
              <span role="alert" className="block text-xs font-normal text-status-down">
                {errors.name}
              </span>
            )}
          </label>
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Algorithm")}
            <select
              aria-label={t("Algorithm")}
              className={INPUT}
              disabled={!write.allowed}
              value={f.algorithm}
              onChange={(e) => set({ algorithm: e.target.value as PoolForm["algorithm"] })}
            >
              {Object.entries(ALGO).map(([k, v]) => (
                <option key={k} value={k}>
                  {t(v)}
                </option>
              ))}
            </select>
          </label>
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Health monitor")}
            <Combobox
              aria-label={t("Health monitor")}
              disabled={!write.allowed}
              value={f.health_monitor_id ?? ""}
              onChange={(v) => set({ health_monitor_id: v || null })}
              options={[
                { value: "", label: t("None") },
                ...(monitors.data?.items ?? []).map((m) => ({
                  value: m.id,
                  label: `${m.name} · ${m.type.toUpperCase()}`,
                })),
              ]}
            />
          </label>
          <div className="space-y-1.5">
            <div className="text-sm font-medium">{t("Backend TLS")}</div>
            <div className="flex h-9 items-center gap-5 text-sm">
              <label className="flex items-center gap-2">
                <Switch
                  aria-label={t("Backend TLS")}
                  checked={f.tls}
                  disabled={!write.allowed}
                  onCheckedChange={(on) => set({ tls: on, tls_verify: on ? f.tls_verify : false })}
                />
                TLS
              </label>
              <label className="flex items-center gap-2">
                <Switch
                  aria-label={t("TLS verify")}
                  checked={f.tls_verify}
                  disabled={!write.allowed || !f.tls}
                  onCheckedChange={(on) => set({ tls_verify: on })}
                />
                {t("Verify")}
              </label>
            </div>
          </div>
        </div>
        <fieldset className="space-y-3 rounded-lg border p-4">
          <legend className="px-1 text-sm font-medium">{t("Persistence")}</legend>
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="block space-y-1.5 text-sm font-medium">
              {t("Type")}
              <select
                aria-label={t("Persistence type")}
                className={INPUT}
                disabled={!write.allowed}
                value={f.persistence ? "source_ip" : "none"}
                onChange={(e) =>
                  set({
                    persistence:
                      e.target.value === "none"
                        ? null
                        : { type: "source_ip", table_size: 100000, timeout_s: 1800 },
                  })
                }
              >
                <option value="none">{t("None")}</option>
                <option value="source_ip">{t("Source IP")}</option>
              </select>
            </label>
            <label className="block space-y-1.5 text-sm font-medium">
              {t("Table size")}
              <input
                aria-label={t("Table size")}
                type="number"
                className={INPUT}
                disabled={!write.allowed || !f.persistence}
                value={f.persistence?.table_size ?? ""}
                onChange={(e) =>
                  f.persistence &&
                  set({ persistence: { ...f.persistence, table_size: Number(e.target.value) } })
                }
              />
            </label>
            <label className="block space-y-1.5 text-sm font-medium">
              {t("Timeout (s)")}
              <input
                aria-label={t("Persistence timeout")}
                type="number"
                className={INPUT}
                disabled={!write.allowed || !f.persistence}
                value={f.persistence?.timeout_s ?? ""}
                onChange={(e) =>
                  f.persistence &&
                  set({ persistence: { ...f.persistence, timeout_s: Number(e.target.value) } })
                }
              />
            </label>
          </div>
          <p className="text-xs text-muted-foreground">
            {t("Source IP is the only persistence type in phase 1.")}
          </p>
        </fieldset>
        <fieldset className="space-y-3 rounded-lg border p-4">
          <legend className="px-1 text-sm font-medium">{t("Passive check and slow start")}</legend>
          {!f.health_monitor_id && (
            <p className="text-xs text-muted-foreground">
              {t("Both need a health monitor on this pool.")}
            </p>
          )}
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              aria-label={t("Judge members by their real traffic")}
              disabled={!write.allowed || !f.health_monitor_id}
              checked={f.passive_check != null}
              onChange={(e) =>
                set({
                  passive_check: e.target.checked
                    ? { observe: "layer7", error_limit: 10, on_error: "mark-down" }
                    : null,
                })
              }
            />
            {t("Judge members by their real traffic as well as the monitor")}
          </label>
          {f.passive_check && (
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="block space-y-1.5 text-sm font-medium">
                {t("Observe")}
                <select
                  aria-label={t("Observe")}
                  className={INPUT}
                  disabled={!write.allowed}
                  value={f.passive_check.observe}
                  onChange={(e) =>
                    set({
                      passive_check: {
                        ...f.passive_check!,
                        observe: e.target.value as PassiveCheck["observe"],
                      },
                    })
                  }
                >
                  <option value="layer4">{t("Connections (layer 4)")}</option>
                  <option value="layer7">{t("HTTP responses (layer 7)")}</option>
                </select>
              </label>
              <label className="block space-y-1.5 text-sm font-medium">
                {t("Error limit")}
                <input
                  aria-label={t("Error limit")}
                  type="number"
                  min={1}
                  max={1000}
                  className={INPUT}
                  disabled={!write.allowed}
                  value={f.passive_check.error_limit}
                  onChange={(e) =>
                    set({
                      passive_check: { ...f.passive_check!, error_limit: Number(e.target.value) },
                    })
                  }
                />
              </label>
              <label className="block space-y-1.5 text-sm font-medium">
                {t("On error")}
                <select
                  aria-label={t("On error")}
                  className={INPUT}
                  disabled={!write.allowed}
                  value={f.passive_check.on_error}
                  onChange={(e) =>
                    set({
                      passive_check: {
                        ...f.passive_check!,
                        on_error: e.target.value as PassiveCheck["on_error"],
                      },
                    })
                  }
                >
                  <option value="mark-down">{t("Mark down")}</option>
                  <option value="fastinter">{t("Check faster")}</option>
                  <option value="fail-check">{t("Count a failed check")}</option>
                  <option value="sudden-death">{t("Fail and mark down")}</option>
                </select>
              </label>
            </div>
          )}
          <label className="block max-w-xs space-y-1.5 text-sm font-medium">
            {t("Slow start (s)")}
            <input
              aria-label={t("Slow start")}
              type="number"
              min={1}
              max={3600}
              className={INPUT}
              disabled={!write.allowed || !f.health_monitor_id}
              value={f.slow_start_s ?? ""}
              onChange={(e) =>
                set({ slow_start_s: e.target.value ? Number(e.target.value) : null })
              }
            />
            <span className="block text-xs font-normal text-muted-foreground">
              {t("A member that comes back takes traffic gradually over this time. Empty is off.")}
            </span>
          </label>
        </fieldset>
        <section className="space-y-2">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-medium">{t("Members")}</h3>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="ml-auto"
              disabled={!write.allowed}
              onClick={() =>
                set({
                  members: [
                    ...f.members,
                    {
                      id: crypto.randomUUID(),
                      name: `${f.name || "member"}-${f.members.length + 1}`,
                      address: "",
                      port: 8080,
                      weight: 100,
                      admin_state: "enabled",
                    },
                  ],
                })
              }
            >
              {t("Add member")}
            </Button>
          </div>
          {errors.members && (
            <p role="alert" className="text-xs text-status-down">
              {errors.members}
            </p>
          )}
          <div className="rounded-lg border">
            <div className="grid grid-cols-[1fr_1.2fr_5rem_5rem_6.5rem_auto] gap-2 border-b bg-muted/60 px-3 py-2 text-xs text-muted-foreground">
              <span>{t("Name")}</span>
              <span>{t("Address")}</span>
              <span>{t("Port")}</span>
              <span>{t("Weight")}</span>
              <span>{t("Admin state")}</span>
              <span />
            </div>
            {f.members.map((m, i) => {
              const isNew = pool !== "new" && !pool.members.some((x) => x.id === m.id);
              return (
                <div
                  key={m.id}
                  className={cn(
                    "grid grid-cols-[1fr_1.2fr_5rem_5rem_6.5rem_auto] items-center gap-2 border-b px-3 py-2 last:border-b-0",
                    isNew && "bg-draft-bg",
                  )}
                >
                  <input
                    aria-label={t("Member {n} name", { n: i + 1 })}
                    className={INPUT}
                    disabled={!write.allowed}
                    value={m.name ?? ""}
                    onChange={(e) => setMember(i, { name: e.target.value })}
                  />
                  <input
                    aria-label={t("Member {n} address", { n: i + 1 })}
                    className={cn(INPUT, "font-mono")}
                    disabled={!write.allowed}
                    value={m.address}
                    onChange={(e) => setMember(i, { address: e.target.value })}
                  />
                  <input
                    aria-label={t("Member {n} port", { n: i + 1 })}
                    type="number"
                    className={cn(INPUT, "font-mono")}
                    disabled={!write.allowed}
                    value={m.port ?? ""}
                    onChange={(e) =>
                      setMember(i, { port: e.target.value ? Number(e.target.value) : null })
                    }
                  />
                  <input
                    aria-label={t("Member {n} weight", { n: i + 1 })}
                    type="number"
                    className={INPUT}
                    disabled={!write.allowed}
                    value={m.weight}
                    onChange={(e) => setMember(i, { weight: Number(e.target.value) })}
                  />
                  <select
                    aria-label={t("Member {n} state", { n: i + 1 })}
                    className={INPUT}
                    disabled={!write.allowed}
                    value={m.admin_state}
                    onChange={(e) =>
                      setMember(i, { admin_state: e.target.value as Member["admin_state"] })
                    }
                  >
                    <option value="enabled">{t("Enabled")}</option>
                    <option value="disabled">{t("Disabled")}</option>
                    <option value="drain">{t("Drain")}</option>
                  </select>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={!write.allowed}
                    onClick={() => set({ members: f.members.filter((_, j) => j !== i) })}
                  >
                    {t("Remove")}
                  </Button>
                  {Object.entries(errors)
                    .filter(([p]) => p.startsWith(`members.${i}`))
                    .map(([p, msg]) => (
                      <span key={p} role="alert" className="col-span-6 text-xs text-status-down">
                        {msg}
                      </span>
                    ))}
                </div>
              );
            })}
          </div>
          <div className="flex gap-2 rounded-lg bg-muted/60 p-3 text-xs text-muted-foreground">
            <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
            <p>
              {t(
                "Admin state here is desired config and goes through the change set. To drain or enable a member right now, use the Live buttons on the virtual service.",
              )}
            </p>
          </div>
        </section>
      </Sheet>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete pool {name}?", { name: pool === "new" ? "" : pool.name })}
        confirmLabel={t("Add delete to draft")}
        busy={save.isPending}
        onConfirm={async () => {
          if (pool === "new") return;
          try {
            await save.mutateAsync({ action: "delete", id: pool.id, version: pool.version });
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
            "The pool is removed when the change set is applied. If a service still uses it, the validator blocks the apply.",
          )}
        </p>
      </ConfirmDialog>
    </>
  );
}

/** Pools: members' admin state, algorithm, monitor, persistence, and which services use each. */
export function Pools({ tenant, q = "" }: { tenant: string; q?: string }) {
  const { draft } = useDraft(tenant);
  const pools = usePools(tenant, draft?.id);
  const monitors = useMonitors(tenant, draft?.id);
  const services = useServices(tenant, draft?.id);
  const can = useCan(tenant);
  const write = can("write", "application");
  const navigate = useNavigate();
  const [editing, setEditing] = useState<PoolRead | "new" | null>(null);

  const monitorName = new Map(monitors.data?.items.map((m) => [m.id, m.name]));
  const usedBy = new Map<string, string[]>();
  for (const v of services.data?.items ?? []) {
    for (const id of new Set([v.default_pool_id, ...v.routes.map((r) => r.pool_id)])) {
      if (id) usedBy.set(id, [...(usedBy.get(id) ?? []), v.name]);
    }
  }
  const rows = (pools.data?.items ?? []).filter((p) =>
    p.name.toLowerCase().includes(q.toLowerCase()),
  );

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("Pools")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("{n} pools", { n: pools.data?.items.length ?? 0 })}
          </p>
        </div>
        <Tip label={write.reason}>
          <Button disabled={!write.allowed} onClick={() => setEditing("new")}>
            <Plus /> {t("New pool")}
          </Button>
        </Tip>
      </div>
      <input
        aria-label={t("Filter by name")}
        placeholder={t("Filter by name")}
        className={cn(INPUT, "w-64")}
        defaultValue={q}
        onChange={(e) =>
          void navigate({
            to: "/t/$tenant/pools",
            params: { tenant },
            search: { q: e.target.value || undefined },
            replace: true,
          })
        }
      />
      <div className="overflow-x-auto rounded-lg border bg-card">
        {pools.isPending ? (
          <TableSkeleton cols={7} />
        ) : pools.isError ? (
          <ErrorState error={pools.error} onRetry={() => void pools.refetch()} />
        ) : rows.length === 0 ? (
          q ? (
            <EmptyState title={t("No pools match")} body={t("Clear the filter to see more.")} />
          ) : (
            <EmptyState
              title={t("No pools yet")}
              body={t("A pool is a set of backend servers a virtual service sends traffic to.")}
              action={
                write.allowed ? (
                  <Button onClick={() => setEditing("new")}>{t("Create a pool")}</Button>
                ) : undefined
              }
            />
          )
        ) : (
          <table className="w-full text-[13px]">
            <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {[
                  "Name",
                  "Algorithm",
                  "Members",
                  "Health monitor",
                  "Persistence",
                  "Backend TLS",
                  "Used by",
                ].map((h) => (
                  <th key={h} className="px-4 py-2.5 font-medium">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((p) => {
                const states = { enabled: 0, disabled: 0, drain: 0 };
                for (const m of p.members) states[m.admin_state]++;
                return (
                  <tr key={p.id}>
                    <td className="px-4 py-3">
                      <span className="flex items-center gap-2">
                        {p.draft_status !== "unchanged" && <DraftMarker status={p.draft_status} />}
                        <button
                          type="button"
                          className={cn(
                            "font-medium text-primary hover:underline",
                            p.draft_status === "deleted" && "line-through",
                          )}
                          onClick={() => setEditing(p)}
                        >
                          {p.name}
                        </button>
                      </span>
                    </td>
                    <td className="px-4 py-3">{t(ALGO[p.algorithm] ?? p.algorithm)}</td>
                    <td className="px-4 py-3">
                      <span className="flex flex-wrap gap-1.5">
                        <span>{t("{n} members", { n: p.members.length })}</span>
                        {states.drain > 0 && (
                          <StatusPill status="draining">
                            {t("{n} draining", { n: states.drain })}
                          </StatusPill>
                        )}
                        {states.disabled > 0 && (
                          <StatusPill status="neutral">
                            {t("{n} disabled", { n: states.disabled })}
                          </StatusPill>
                        )}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {p.health_monitor_id ? (monitorName.get(p.health_monitor_id) ?? "—") : "—"}
                    </td>
                    <td className="px-4 py-3">{persistenceLabel(toForm(p).persistence)}</td>
                    <td className="px-4 py-3">
                      {p.tls ? (p.tls_verify ? t("On · verify") : t("On")) : t("Off")}
                    </td>
                    <td className="px-4 py-3">{usedBy.get(p.id)?.join(", ") ?? "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      {editing && (
        <PoolSheet
          key={
            editing === "new" ? "new" : `${editing.id}-${editing.version}-${editing.draft_status}`
          }
          tenant={tenant}
          pool={editing}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}
