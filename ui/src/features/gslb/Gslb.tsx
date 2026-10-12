/**
 * GSLB: wide IPs answered by PowerDNS (ADR 0160 to 0165). Zones are handed to a tenant by a system
 * administrator; tenant admins manage wide IPs, DNSSEC and the sync. All changes are live and audited.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useMe } from "@/auth/session";
import { ChipInput } from "@/components/sy/ChipInput";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { cidrProblem } from "@/lib/ip";
import { shortWhen } from "@/lib/format";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Zone = components["schemas"]["GslbZoneRead"];
type WideIp = components["schemas"]["WideIpRead"];
type Member = components["schemas"]["WideIpMember"];
type Health = components["schemas"]["WideIpHealth"];
type State = components["schemas"]["WideIpState"]["state"];
type Algorithm = components["schemas"]["WideIpIn"]["algorithm"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

export const ALGORITHMS: { value: Algorithm; label: string; help: string }[] = [
  { value: "failover", label: "Failover", help: "The first priority tier that has a member up." },
  { value: "all", label: "All healthy", help: "Every member that is up." },
  { value: "weighted", label: "Weighted", help: "One healthy member, picked by weight." },
  { value: "subnet", label: "By client subnet", help: "Members whose subnets contain the client." },
  { value: "geo", label: "Nearest (GeoIP)", help: "The closest member; needs a GeoIP database." },
  {
    value: "latency",
    label: "Lowest latency",
    help: "The member with the lowest round trip from the controller.",
  },
];

/** Pill tone for a wide IP's sync state. */
export function stateTone(s: State): "healthy" | "degraded" | "down" | "neutral" | "pending" {
  if (s === "in_sync") return "healthy";
  if (s === "drift") return "degraded";
  if (s === "error") return "down";
  if (s === "pending") return "pending";
  return "neutral";
}

const STATE_LABEL: Record<State, string> = {
  in_sync: "In sync",
  drift: "Drift",
  pending: "Pending",
  error: "Error",
  off: "Off",
  unknown: "Unknown",
};

/** Blank member for the editor. */
export const newMember = (): Member => ({
  address: "",
  virtual_service: null,
  site: "",
  weight: 1,
  priority: 1,
  subnets: [],
});

function useRoles(tenant: string) {
  const me = useMe().data;
  const role = me?.tenants.find((x) => x.name === tenant)?.role;
  const admin = !!me?.is_system || role === "tenant-admin" || role === "system-admin";
  return { admin, system: !!me?.is_system };
}

function WideIpSheet({
  tenant,
  zones,
  wide,
  zone: initialZone,
  onClose,
}: {
  tenant: string;
  zones: Zone[];
  wide: WideIp | null;
  zone: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = wide !== null;
  const vss = useQuery({
    queryKey: ["t", tenant, "virtual-services", "names"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/virtual-services", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
  const [zone, setZone] = useState(initialZone);
  const [name, setName] = useState(wide?.name ?? "");
  const [algorithm, setAlgorithm] = useState<Algorithm>(
    (wide?.algorithm as Algorithm) ?? "failover",
  );
  const [ttl, setTtl] = useState(wide?.ttl ?? 30);
  const [enabled, setEnabled] = useState(wide?.enabled ?? true);
  const [health, setHealth] = useState<Health>(
    wide?.health ?? { kind: "tcp", port: 443, timeout_ms: 1000 },
  );
  const [members, setMembers] = useState<Member[]>(wide?.members ?? [newMember()]);
  const [problem, setProblem] = useState<unknown>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [confirmDelete, setConfirmDelete] = useState(false);

  const setMember = (i: number, patch: Partial<Member>) =>
    setMembers((ms) => ms.map((m, j) => (j === i ? { ...m, ...patch } : m)));
  const clean = (m: Member): Member => ({
    address: m.virtual_service ? null : m.address || null,
    virtual_service: m.virtual_service || null,
    site: m.site || null,
    weight: m.weight,
    priority: m.priority,
    subnets: m.subnets ?? [],
  });
  const done = () => {
    void qc.invalidateQueries({ queryKey: ["t", tenant, "gslb"] });
    onClose();
  };
  const fail = (e: unknown) => {
    if (e instanceof ApiError && e.status === 422) {
      const m: Record<string, string> = {};
      for (const x of e.problem?.errors ?? []) m[x.path] = x.message;
      setErrors(m);
    } else setProblem(e);
  };
  const save = useMutation({
    mutationFn: async () => {
      setProblem(null);
      setErrors({});
      const h: Health =
        health.kind === "http"
          ? { ...health, scheme: health.scheme ?? "https", path: health.path || "/" }
          : { kind: "tcp", port: health.port, timeout_ms: health.timeout_ms };
      const body = { algorithm, ttl, enabled, health: h, members: members.map(clean) };
      if (!wide)
        return unwrap(
          await api.POST("/api/v1/tenants/{t}/gslb-zones/{zone}/wide-ips", {
            params: { path: { t: tenant, zone } },
            body: { name, ...body },
          }),
        );
      return unwrap(
        await api.PATCH("/api/v1/tenants/{t}/gslb-zones/{zone}/wide-ips/{name}", {
          params: { path: { t: tenant, zone: wide.zone, name: wide.name } },
          body,
        }),
      );
    },
    onSuccess: done,
    onError: fail,
  });
  const remove = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.DELETE("/api/v1/tenants/{t}/gslb-zones/{zone}/wide-ips/{name}", {
          params: { path: { t: tenant, zone: wide!.zone, name: wide!.name } },
        }),
      ),
    onSuccess: done,
    onError: (e) => {
      setConfirmDelete(false);
      setProblem(e);
    },
  });
  const err = (k: string) =>
    errors[k] ? (
      <span role="alert" className="block text-xs font-normal text-status-down">
        {errors[k]}
      </span>
    ) : null;
  const usesWeight = algorithm === "weighted";
  const usesPriority = algorithm === "failover";
  const usesSubnets = algorithm === "subnet";
  const valid = (editing || (name !== "" && zone !== "")) && members.length > 0;

  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={editing ? t("Edit {name}", { name: wide.fqdn }) : t("New wide IP")}
      badge={<StatusPill status="pending">{t("Live")}</StatusPill>}
      footer={
        <>
          {editing && (
            <Button variant="ghost" onClick={() => setConfirmDelete(true)}>
              {t("Delete")}
            </Button>
          )}
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button disabled={!valid || save.isPending} onClick={() => save.mutate()}>
            {editing ? t("Save") : t("Create")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      {!editing && (
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="space-y-1.5 text-sm font-medium">
            {t("Name")}
            <input
              aria-label={t("Name")}
              className={cn(INPUT, "font-mono")}
              placeholder="shop"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            {err("name")}
          </label>
          <div className="space-y-1.5">
            <div className="text-sm font-medium">{t("Zone")}</div>
            <Combobox
              aria-label={t("Zone")}
              value={zone}
              onChange={setZone}
              placeholder={t("Choose a zone")}
              options={zones.map((z) => ({ value: z.name, label: z.name }))}
            />
          </div>
        </div>
      )}
      <fieldset className="space-y-1.5">
        <legend className="text-sm font-medium">{t("Answers with")}</legend>
        <div className="grid gap-2 sm:grid-cols-2">
          {ALGORITHMS.map((a) => (
            <label
              key={a.value}
              className={cn(
                "flex cursor-pointer items-start gap-2 rounded-lg border bg-card p-2.5 text-sm",
                algorithm === a.value && "border-primary bg-primary/10",
              )}
            >
              <input
                type="radio"
                name="gslb-algorithm"
                aria-label={t(a.label)}
                className="mt-1"
                checked={algorithm === a.value}
                onChange={() => setAlgorithm(a.value)}
              />
              <span>
                <span className="font-medium">{t(a.label)}</span>
                <span className="block text-xs text-muted-foreground">{t(a.help)}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1.5 text-sm font-medium">
          {t("TTL (s)")}
          <input
            aria-label={t("TTL (s)")}
            type="number"
            min={1}
            className={INPUT}
            value={ttl}
            onChange={(e) => setTtl(Number(e.target.value))}
          />
          {err("ttl")}
        </label>
        <label className="flex items-center gap-2 self-end pb-2 text-sm">
          <Switch aria-label={t("Enabled")} checked={enabled} onCheckedChange={setEnabled} />
          {t("Enabled")}
        </label>
      </div>
      <fieldset className="space-y-2 rounded-lg border p-3">
        <legend className="px-1 text-sm font-medium">{t("Health check")}</legend>
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="space-y-1.5 text-sm">
            {t("Check")}
            <select
              aria-label={t("Check")}
              className={INPUT}
              value={health.kind}
              onChange={(e) => setHealth({ ...health, kind: e.target.value as "tcp" | "http" })}
            >
              <option value="tcp">TCP</option>
              <option value="http">HTTP</option>
            </select>
          </label>
          <label className="space-y-1.5 text-sm">
            {t("Port")}
            <input
              aria-label={t("Check port")}
              type="number"
              className={INPUT}
              value={health.port}
              onChange={(e) => setHealth({ ...health, port: Number(e.target.value) })}
            />
          </label>
          <label className="space-y-1.5 text-sm">
            {t("Timeout (ms)")}
            <input
              aria-label={t("Check timeout (ms)")}
              type="number"
              className={INPUT}
              value={health.timeout_ms}
              onChange={(e) => setHealth({ ...health, timeout_ms: Number(e.target.value) })}
            />
          </label>
        </div>
        {health.kind === "http" && (
          <div className="grid gap-3 sm:grid-cols-3">
            <label className="space-y-1.5 text-sm">
              {t("Scheme")}
              <select
                aria-label={t("Check scheme")}
                className={INPUT}
                value={health.scheme ?? "https"}
                onChange={(e) =>
                  setHealth({ ...health, scheme: e.target.value as "http" | "https" })
                }
              >
                <option value="https">https</option>
                <option value="http">http</option>
              </select>
            </label>
            <label className="space-y-1.5 text-sm">
              {t("Path")}
              <input
                aria-label={t("Check path")}
                className={cn(INPUT, "font-mono")}
                placeholder="/healthz"
                value={health.path ?? ""}
                onChange={(e) => setHealth({ ...health, path: e.target.value })}
              />
            </label>
            <label className="space-y-1.5 text-sm">
              {t("Body must contain")}
              <input
                aria-label={t("Check match")}
                className={INPUT}
                value={health.match ?? ""}
                onChange={(e) => setHealth({ ...health, match: e.target.value || null })}
              />
            </label>
          </div>
        )}
      </fieldset>
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <div className="text-sm font-medium">{t("Members")}</div>
          <Button size="sm" variant="outline" onClick={() => setMembers([...members, newMember()])}>
            {t("Add member")}
          </Button>
        </div>
        {err("members")}
        {members.map((m, i) => (
          <div key={i} className="space-y-2 rounded-lg border p-3">
            <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]">
              <div>
                <Combobox
                  aria-label={t("Member {n} virtual service", { n: i + 1 })}
                  value={m.virtual_service ?? ""}
                  onChange={(v) => setMember(i, { virtual_service: v || null })}
                  placeholder={t("Virtual service")}
                  options={[
                    { value: "", label: t("None (use an address)") },
                    ...(vss.data?.items ?? []).map((v) => ({ value: v.name, label: v.name })),
                  ]}
                />
              </div>
              <input
                aria-label={t("Member {n} address", { n: i + 1 })}
                className={cn(INPUT, "font-mono")}
                placeholder="2001:db8::10"
                disabled={!!m.virtual_service}
                value={m.address ?? ""}
                onChange={(e) => setMember(i, { address: e.target.value })}
              />
              <Button
                size="sm"
                variant="ghost"
                aria-label={t("Remove member {n}", { n: i + 1 })}
                onClick={() => setMembers(members.filter((_, j) => j !== i))}
              >
                {t("Remove")}
              </Button>
            </div>
            <div className="grid gap-2 text-xs sm:grid-cols-3">
              <label className="space-y-1">
                {t("Site")}
                <input
                  aria-label={t("Member {n} site", { n: i + 1 })}
                  className={INPUT}
                  value={m.site ?? ""}
                  onChange={(e) => setMember(i, { site: e.target.value })}
                />
              </label>
              {usesPriority && (
                <label className="space-y-1">
                  {t("Priority")}
                  <input
                    aria-label={t("Member {n} priority", { n: i + 1 })}
                    type="number"
                    min={1}
                    className={INPUT}
                    value={m.priority}
                    onChange={(e) => setMember(i, { priority: Number(e.target.value) })}
                  />
                </label>
              )}
              {usesWeight && (
                <label className="space-y-1">
                  {t("Weight")}
                  <input
                    aria-label={t("Member {n} weight", { n: i + 1 })}
                    type="number"
                    min={0}
                    className={INPUT}
                    value={m.weight}
                    onChange={(e) => setMember(i, { weight: Number(e.target.value) })}
                  />
                </label>
              )}
            </div>
            {usesSubnets && (
              <div className="space-y-1 text-xs">
                {t("Client subnets (empty is the default member)")}
                <ChipInput
                  label={t("Member {n} subnets", { n: i + 1 })}
                  freeText={{ validate: (v) => (cidrProblem(v) ? t("Not a valid network") : null) }}
                  value={m.subnets ?? []}
                  onChange={(v) => setMember(i, { subnets: v })}
                  placeholder="10.0.0.0/8"
                />
              </div>
            )}
            {err(`members.${i}`)}
          </div>
        ))}
      </div>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete {name}?", { name: wide?.fqdn ?? "" })}
        confirmLabel={t("Delete")}
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
      >
        <p>{t("The name stops answering within one TTL. This is audited.")}</p>
      </ConfirmDialog>
    </Sheet>
  );
}

function ZoneSheet({ tenant, onClose }: { tenant: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [ns, setNs] = useState<string[]>([]);
  const create = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/gslb-zones", {
          params: { path: { t: tenant } },
          body: { name, nameservers: ns },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["t", tenant, "gslb"] });
      onClose();
    },
  });
  return (
    <Sheet
      open
      centered
      onOpenChange={(o) => !o && onClose()}
      title={t("Add a zone")}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            disabled={!name || ns.length === 0 || create.isPending}
            onClick={() => create.mutate()}
          >
            {t("Add zone")}
          </Button>
        </>
      }
    >
      {create.error && <ErrorState error={create.error} />}
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Zone")}
        <input
          aria-label={t("Zone name")}
          className={cn(INPUT, "font-mono")}
          placeholder="gslb.example.com"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <div className="space-y-1.5 text-sm font-medium">
        {t("Nameservers")}
        <ChipInput
          label={t("Nameservers")}
          freeText={{ validate: () => null }}
          value={ns}
          onChange={setNs}
          placeholder="ns1.example.com"
        />
      </div>
    </Sheet>
  );
}

function ZoneRow({
  tenant,
  zone,
  admin,
  system,
}: {
  tenant: string;
  zone: Zone;
  admin: boolean;
  system: boolean;
}) {
  const qc = useQueryClient();
  const [ds, setDs] = useState<string[] | null>(null);
  const [confirm, setConfirm] = useState(false);
  const refresh = () => void qc.invalidateQueries({ queryKey: ["t", tenant, "gslb"] });
  const sign = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/gslb-zones/{zone}/dnssec", {
          params: { path: { t: tenant, zone: zone.name } },
        }),
      ),
    onSuccess: (r) => {
      setDs(r.ds);
      refresh();
    },
  });
  const showDs = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/gslb-zones/{zone}/ds", {
          params: { path: { t: tenant, zone: zone.name } },
        }),
      ),
    onSuccess: (r) => setDs(r.ds),
  });
  const remove = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.DELETE("/api/v1/tenants/{t}/gslb-zones/{zone}", {
          params: { path: { t: tenant, zone: zone.name } },
        }),
      ),
    onSuccess: () => {
      setConfirm(false);
      refresh();
    },
  });
  return (
    <li className="space-y-2 rounded-lg border bg-card p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono font-medium">{zone.name}</span>
        <span className="text-xs text-muted-foreground">{zone.nameservers.join(", ")}</span>
        <StatusPill status={zone.dnssec ? "healthy" : "neutral"}>
          {zone.dnssec ? t("DNSSEC on") : t("Unsigned")}
        </StatusPill>
        <span className="ml-auto flex gap-2">
          {zone.dnssec ? (
            <Button size="sm" variant="outline" onClick={() => showDs.mutate()}>
              {t("Show DS records")}
            </Button>
          ) : (
            <Tip label={admin ? null : t("Needs tenant-admin")}>
              <Button
                size="sm"
                variant="outline"
                disabled={!admin || sign.isPending}
                onClick={() => sign.mutate()}
              >
                {t("Enable DNSSEC")}
              </Button>
            </Tip>
          )}
          {system && (
            <Button size="sm" variant="ghost" onClick={() => setConfirm(true)}>
              {t("Remove zone")}
            </Button>
          )}
        </span>
      </div>
      {(sign.error || showDs.error || remove.error) && (
        <ErrorState error={sign.error ?? showDs.error ?? remove.error} />
      )}
      {ds && (
        <div className="space-y-1">
          <p className="text-xs text-muted-foreground">
            {t("Publish these DS records at the parent zone.")}
          </p>
          <pre
            aria-label={t("DS records")}
            className="overflow-x-auto rounded-md bg-muted p-2 font-mono text-xs"
          >
            {ds.join("\n")}
          </pre>
        </div>
      )}
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={t("Remove {name}?", { name: zone.name })}
        confirmLabel={t("Remove zone")}
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
      >
        <p>{t("The zone and its wide IPs are removed from PowerDNS. This is audited.")}</p>
      </ConfirmDialog>
    </li>
  );
}

export function Gslb({ tenant }: { tenant: string }) {
  const { admin, system } = useRoles(tenant);
  const qc = useQueryClient();
  const zones = useQuery({
    queryKey: ["t", tenant, "gslb", "zones"],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/gslb-zones", { params: { path: { t: tenant } } })),
  });
  const wides = useQuery({
    queryKey: ["t", tenant, "gslb", "wide-ips"],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/wide-ips", { params: { path: { t: tenant } } })),
  });
  const status = useQuery({
    queryKey: ["t", tenant, "gslb", "status"],
    refetchInterval: 30_000,
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/gslb/status", { params: { path: { t: tenant } } })),
  });
  const sync = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/v1/tenants/{t}/gslb/sync", { params: { path: { t: tenant } } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["t", tenant, "gslb"] }),
  });
  const [sheet, setSheet] = useState<WideIp | "new" | null>(null);
  const [zoneSheet, setZoneSheet] = useState(false);
  const stateOf = new Map((status.data ?? []).map((s) => [s.fqdn, s]));
  const zoneList = zones.data ?? [];
  const reason = admin ? null : t("Needs tenant-admin");
  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("GSLB")}</h1>
          <p className="text-sm text-muted-foreground">
            {t(
              "A wide IP is a DNS name that answers with whichever of its members are healthy, usually the VIPs of one service at several sites.",
            )}
          </p>
        </div>
        <div className="flex gap-2">
          <Tip label={reason}>
            <Button
              variant="outline"
              disabled={!admin || sync.isPending}
              onClick={() => sync.mutate()}
            >
              {t("Sync now")}
            </Button>
          </Tip>
          <Tip label={reason ?? (zoneList.length === 0 ? t("Add a zone first") : null)}>
            <Button disabled={!admin || zoneList.length === 0} onClick={() => setSheet("new")}>
              {t("New wide IP")}
            </Button>
          </Tip>
        </div>
      </div>
      {sync.data && (
        <p role="status" className="text-sm">
          {t("{w} records written, {f} failed", { w: sync.data.written, f: sync.data.failed })}
        </p>
      )}
      {sync.error && <ErrorState error={sync.error} />}
      <section aria-label={t("Wide IPs")} className="overflow-x-auto rounded-lg border bg-card">
        {wides.isPending ? (
          <TableSkeleton cols={6} />
        ) : wides.isError ? (
          <ErrorState error={wides.error} onRetry={() => void wides.refetch()} />
        ) : (wides.data ?? []).length === 0 ? (
          <EmptyState
            title={t("No wide IPs")}
            body={t("Create one to answer a name with the healthy members of a service.")}
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["Name", "Answers with", "Members", "TTL", "State", "Applied"].map((h) => (
                  <th key={h} className="px-4 py-2.5 font-medium">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {(wides.data ?? []).map((w) => {
                const st = stateOf.get(w.fqdn);
                const s: State = !w.enabled ? "off" : (st?.state ?? "unknown");
                return (
                  <tr key={w.id}>
                    <td className="px-4 py-3">
                      <button
                        type="button"
                        className="font-mono font-medium text-primary hover:underline"
                        onClick={() => setSheet(w)}
                      >
                        {w.fqdn}
                      </button>
                    </td>
                    <td className="px-4 py-3">
                      {t(ALGORITHMS.find((a) => a.value === w.algorithm)?.label ?? w.algorithm)}
                    </td>
                    <td className="px-4 py-3">{w.members.length}</td>
                    <td className="px-4 py-3">{w.ttl} s</td>
                    <td className="px-4 py-3">
                      <StatusPill status={stateTone(s)}>{t(STATE_LABEL[s])}</StatusPill>
                      {(st?.last_error ?? w.last_error) && (
                        <span className="block text-xs text-status-down">
                          {st?.last_error ?? w.last_error}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-muted-foreground">
                      {w.applied_at ? shortWhen(w.applied_at) : "-"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>
      <section aria-label={t("Zones")} className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">{t("Zones")}</h2>
          <Tip label={system ? null : t("Only a system administrator adds a zone")}>
            <Button
              size="sm"
              variant="outline"
              disabled={!system}
              onClick={() => setZoneSheet(true)}
            >
              {t("Add zone")}
            </Button>
          </Tip>
        </div>
        {zones.isError ? (
          <ErrorState error={zones.error} />
        ) : zoneList.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {t("No zone is assigned to this tenant. A system administrator hands one over.")}
          </p>
        ) : (
          <ul className="space-y-2">
            {zoneList.map((z) => (
              <ZoneRow key={z.id} tenant={tenant} zone={z} admin={admin} system={system} />
            ))}
          </ul>
        )}
      </section>
      {sheet !== null && (
        <WideIpSheet
          key={sheet === "new" ? "new" : sheet.id}
          tenant={tenant}
          zones={zoneList}
          wide={sheet === "new" ? null : sheet}
          zone={sheet === "new" ? (zoneList[0]?.name ?? "") : sheet.zone}
          onClose={() => setSheet(null)}
        />
      )}
      {zoneSheet && <ZoneSheet tenant={tenant} onClose={() => setZoneSheet(false)} />}
    </div>
  );
}
