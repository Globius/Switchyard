// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Routing profiles: how an SE group announces its VIPs, BGP peering or VRRP. Edited through the draft. */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { DraftMarker } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useSaveOp } from "@/features/objects/save";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Profile = components["schemas"]["BgpProfileRead"] | components["schemas"]["VrrpProfileRead"];
type Bgp = components["schemas"]["BgpProfileRead"];
type Vrrp = components["schemas"]["VrrpProfileRead"];
type Peer = {
  peer_ip: string;
  remote_asn: number;
  md5_ref: string | null;
  max_prefixes?: number | null;
};
type Route = { prefix: string; next_hop: string };

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

function useProfiles(tenant: string, changeSet?: string) {
  return useQuery({
    queryKey: ["t", tenant, "routing-profiles", { changeSet }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/routing-profiles", {
          params: { path: { t: tenant }, query: { limit: 200, change_set: changeSet } },
        }),
      ),
  });
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

const isBgp = (p: Profile): p is Bgp => p.kind === "bgp";

/** One line under the profile's name in the list, for example "BGP · AS 65040 · 2 peers · se-edge-a". */
export function profileLine(p: Profile, groups: string[]): string {
  const head = isBgp(p)
    ? t("BGP · AS {asn} · {n} peers", { asn: p.asn, n: p.peers.length })
    : t("VRRP · VRID {vrid}", { vrid: (p as Vrrp).vrid });
  return [head, ...groups].join(" · ");
}

function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
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

function RouteTable({
  routes,
  onChange,
  disabled,
}: {
  routes: Route[];
  onChange: (r: Route[]) => void;
  disabled: boolean;
}) {
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium">{t("Static routes")}</h3>
        <Button
          size="sm"
          variant="outline"
          disabled={disabled}
          onClick={() => onChange([...routes, { prefix: "", next_hop: "" }])}
        >
          {t("Add route")}
        </Button>
      </div>
      {routes.length > 0 && (
        <table className="w-full overflow-hidden rounded-md border text-[13px]">
          <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">{t("Prefix")}</th>
              <th className="px-3 py-2 font-medium">{t("Next hop")}</th>
              <th />
            </tr>
          </thead>
          <tbody className="divide-y">
            {routes.map((r, i) => (
              <tr key={i}>
                {(["prefix", "next_hop"] as const).map((k) => (
                  <td key={k} className="px-3 py-2">
                    <input
                      aria-label={`${k === "prefix" ? t("Prefix") : t("Next hop")} ${i + 1}`}
                      className={cn(INPUT, "font-mono")}
                      disabled={disabled}
                      value={r[k]}
                      onChange={(e) =>
                        onChange(
                          routes.map((x, j) => (j === i ? { ...x, [k]: e.target.value } : x)),
                        )
                      }
                    />
                  </td>
                ))}
                <td className="px-3 py-2 text-right">
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={t("Remove route {n}", { n: i + 1 })}
                    disabled={disabled}
                    onClick={() => onChange(routes.filter((_, j) => j !== i))}
                  >
                    ×
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function Editor({
  tenant,
  profile,
  usedBy,
}: {
  tenant: string;
  profile: Profile;
  usedBy: { id: string; name: string }[];
}) {
  const save = useSaveOp(tenant, "routing_profile");
  const can = useCan(tenant);
  const write = can("write", "application");
  const base = useMemo(() => {
    const out: Record<string, unknown> = { ...profile };
    for (const k of ["id", "version", "tenant_id", "draft_status", "changed_fields", "kind"])
      delete out[k];
    return out;
  }, [profile]);
  const [f, setF] = useState<Record<string, unknown>>(base);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<unknown>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const set = (p: Record<string, unknown>) => setF({ ...f, ...p });
  const patch = Object.fromEntries(
    Object.keys(f)
      .filter((k) => JSON.stringify(f[k]) !== JSON.stringify(base[k]))
      .map((k) => [k, f[k]]),
  );
  const dirty = Object.keys(patch).length > 0;
  const dis = !write.allowed;
  const num = (k: string, label: string, hint?: string) => (
    <Field label={label} hint={hint} error={errors[k]}>
      <input
        aria-label={label}
        type="number"
        className={INPUT}
        disabled={dis}
        value={f[k] as number}
        onChange={(e) => set({ [k]: Number(e.target.value) })}
      />
    </Field>
  );
  const peers = (f.peers as Peer[] | undefined) ?? [];
  async function submit() {
    setErrors({});
    setProblem(null);
    try {
      await save.mutateAsync({
        action: "update",
        id: profile.id,
        version: profile.version,
        body: patch,
      });
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        const m: Record<string, string> = {};
        for (const x of e.problem?.errors ?? []) m[x.path.split(".")[0] ?? x.path] = x.message;
        setErrors(m);
      } else setProblem(e);
    }
  }
  return (
    <div className="space-y-4">
      <section className="space-y-5 rounded-lg border bg-card">
        <header className="flex items-center gap-2 border-b px-4 py-3">
          {profile.draft_status !== "unchanged" && <DraftMarker status={profile.draft_status} />}
          <h2 className="text-lg font-semibold">{profile.name}</h2>
          <Badge>{isBgp(profile) ? "BGP" : "VRRP"}</Badge>
        </header>
        <div className="space-y-5 px-4 pb-4">
          {problem != null && <ErrorState error={problem} />}
          {isBgp(profile) ? (
            <>
              <div className="grid gap-3 sm:grid-cols-[1fr_1fr_1fr_1fr_auto]">
                <Field label={t("Name")} error={errors.name}>
                  <input
                    aria-label={t("Name")}
                    className={INPUT}
                    disabled={dis}
                    value={f.name as string}
                    onChange={(e) => set({ name: e.target.value })}
                  />
                </Field>
                {num("asn", t("Local ASN"))}
                {num("max_paths", t("Max paths"), "1–64")}
                <Field
                  label={t("Max prefixes")}
                  hint={t("Default for peers; empty is no limit")}
                  error={errors.max_prefixes}
                >
                  <input
                    aria-label={t("Max prefixes")}
                    type="number"
                    min={1}
                    className={INPUT}
                    disabled={dis}
                    value={(f.max_prefixes as number | null | undefined) ?? ""}
                    onChange={(e) =>
                      set({ max_prefixes: e.target.value ? Number(e.target.value) : null })
                    }
                  />
                </Field>
                <div className="space-y-1.5 text-sm font-medium">
                  {t("BFD")}
                  <div className="flex h-9 items-center gap-2 font-normal">
                    <Switch
                      aria-label={t("BFD")}
                      checked={f.bfd as boolean}
                      disabled={dis}
                      onCheckedChange={(v) => set({ bfd: v })}
                    />
                    <span className="text-xs text-muted-foreground">
                      {t("Fast failure detection")}
                    </span>
                  </div>
                </div>
              </div>
              <section className="space-y-2">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-medium">{t("Peers")}</h3>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={dis}
                    onClick={() =>
                      set({ peers: [...peers, { peer_ip: "", remote_asn: 0, md5_ref: null }] })
                    }
                  >
                    {t("Add peer")}
                  </Button>
                </div>
                {errors.peers && (
                  <p role="alert" className="text-xs text-status-down">
                    {errors.peers}
                  </p>
                )}
                <table className="w-full overflow-hidden rounded-md border text-[13px]">
                  <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
                    <tr>
                      <th className="px-3 py-2 font-medium">{t("Peer IP")}</th>
                      <th className="px-3 py-2 font-medium">{t("Remote ASN")}</th>
                      <th className="px-3 py-2 font-medium">{t("MD5 password")}</th>
                      <th className="px-3 py-2 font-medium">{t("Max prefixes")}</th>
                      <th />
                    </tr>
                  </thead>
                  <tbody className="divide-y">
                    {peers.map((p, i) => {
                      const upd = (x: Partial<Peer>) =>
                        set({ peers: peers.map((y, j) => (j === i ? { ...y, ...x } : y)) });
                      return (
                        <tr key={i}>
                          <td className="px-3 py-2">
                            <input
                              aria-label={t("Peer IP {n}", { n: i + 1 })}
                              className={cn(INPUT, "font-mono")}
                              disabled={dis}
                              value={p.peer_ip}
                              onChange={(e) => upd({ peer_ip: e.target.value })}
                            />
                          </td>
                          <td className="px-3 py-2">
                            <input
                              aria-label={t("Remote ASN {n}", { n: i + 1 })}
                              type="number"
                              className={INPUT}
                              disabled={dis}
                              value={p.remote_asn}
                              onChange={(e) => upd({ remote_asn: Number(e.target.value) })}
                            />
                          </td>
                          <td className="px-3 py-2">
                            <input
                              aria-label={t("MD5 reference {n}", { n: i + 1 })}
                              placeholder={t("No password")}
                              className={cn(INPUT, "font-mono")}
                              disabled={dis}
                              value={p.md5_ref ?? ""}
                              onChange={(e) => upd({ md5_ref: e.target.value || null })}
                            />
                          </td>
                          <td className="px-3 py-2">
                            <input
                              aria-label={t("Peer {n} max prefixes", { n: i + 1 })}
                              type="number"
                              min={1}
                              placeholder={t("Profile default")}
                              className={INPUT}
                              disabled={dis}
                              value={p.max_prefixes ?? ""}
                              onChange={(e) =>
                                upd({
                                  max_prefixes: e.target.value ? Number(e.target.value) : null,
                                })
                              }
                            />
                          </td>
                          <td className="px-3 py-2 text-right">
                            <Button
                              size="sm"
                              variant="ghost"
                              aria-label={t("Remove peer {n}", { n: i + 1 })}
                              disabled={dis}
                              onClick={() => set({ peers: peers.filter((_, j) => j !== i) })}
                            >
                              ×
                            </Button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                <p className="text-xs text-muted-foreground">
                  {t(
                    "MD5 passwords are OpenBao references. The value itself is never sent to or shown by Switchyard.",
                  )}
                </p>
              </section>
            </>
          ) : (
            <div className="grid gap-3 sm:grid-cols-3">
              <Field label={t("Name")} error={errors.name}>
                <input
                  aria-label={t("Name")}
                  className={INPUT}
                  disabled={dis}
                  value={f.name as string}
                  onChange={(e) => set({ name: e.target.value })}
                />
              </Field>
              {num("vrid", t("VRID"))}
              <Field label={t("Interface")} error={errors.interface}>
                <input
                  aria-label={t("Interface")}
                  className={cn(INPUT, "font-mono")}
                  disabled={dis}
                  value={f.interface as string}
                  onChange={(e) => set({ interface: e.target.value })}
                />
              </Field>
              {num("base_priority", t("Base priority"))}
              {num("priority_step", t("Priority step"))}
              {num("advert_interval_s", t("Advert interval (s)"))}
            </div>
          )}
          <RouteTable
            routes={(f.static_routes as Route[] | undefined) ?? []}
            onChange={(r) => set({ static_routes: r })}
            disabled={dis}
          />
          <div className="flex gap-6 text-sm">
            <span className="text-muted-foreground">{t("Used by")}</span>
            <span className="flex flex-wrap gap-3">
              {usedBy.length === 0
                ? t("No SE group")
                : usedBy.map((g) => (
                    <Link
                      key={g.id}
                      to="/t/$tenant/se-groups"
                      params={{ tenant }}
                      className="text-primary hover:underline"
                    >
                      {g.name}
                    </Link>
                  ))}
            </span>
          </div>
        </div>
      </section>
      <div className="flex items-center justify-end gap-2">
        <Tip label={write.reason}>
          <Button variant="outline" disabled={dis} onClick={() => setConfirmDelete(true)}>
            {t("Delete")}
          </Button>
        </Tip>
        <Button variant="ghost" disabled={!dirty} onClick={() => setF(base)}>
          {t("Revert")}
        </Button>
        <Tip label={write.reason}>
          <Button disabled={dis || !dirty || save.isPending} onClick={() => void submit()}>
            {t("Save to draft")}
          </Button>
        </Tip>
      </div>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete routing profile {name}?", { name: profile.name })}
        confirmLabel={t("Add delete to draft")}
        busy={save.isPending}
        onConfirm={async () => {
          try {
            await save.mutateAsync({ action: "delete", id: profile.id, version: profile.version });
          } catch (e) {
            setProblem(e);
          } finally {
            setConfirmDelete(false);
          }
        }}
      >
        <p>
          {usedBy.length > 0
            ? t(
                "{n} SE groups use this profile; the validator blocks the apply until they use another.",
                {
                  n: usedBy.length,
                },
              )
            : t("No SE group uses this profile.")}
        </p>
      </ConfirmDialog>
    </div>
  );
}

function NewProfile({ tenant, onClose }: { tenant: string; onClose: () => void }) {
  const save = useSaveOp(tenant, "routing_profile");
  const [kind, setKind] = useState<"bgp" | "vrrp">("bgp");
  const [f, setF] = useState({
    name: "",
    asn: "",
    peer_ip: "",
    remote_asn: "",
    vrid: "",
    interface: "",
  });
  const [problem, setProblem] = useState<unknown>(null);
  const body =
    kind === "bgp"
      ? {
          kind,
          name: f.name,
          asn: Number(f.asn),
          peers: [{ peer_ip: f.peer_ip, remote_asn: Number(f.remote_asn), md5_ref: null }],
          static_routes: [],
        }
      : { kind, name: f.name, vrid: Number(f.vrid), interface: f.interface, static_routes: [] };
  const valid =
    f.name !== "" &&
    (kind === "bgp"
      ? Number(f.asn) > 0 && f.peer_ip !== "" && Number(f.remote_asn) > 0
      : Number(f.vrid) > 0 && f.interface !== "");
  const input = (k: keyof typeof f, label: string, mono = false) => (
    <Field label={label}>
      <input
        aria-label={label}
        className={cn(INPUT, mono && "font-mono")}
        value={f[k]}
        onChange={(e) => setF({ ...f, [k]: e.target.value })}
      />
    </Field>
  );
  return (
    <Sheet
      open
      centered
      onOpenChange={(o) => !o && onClose()}
      title={t("New routing profile")}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            disabled={!valid || save.isPending}
            onClick={async () => {
              try {
                await save.mutateAsync({ action: "create", body });
                onClose();
              } catch (e) {
                setProblem(e);
              }
            }}
          >
            {t("Add to draft")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      <div role="radiogroup" aria-label={t("Kind")} className="inline-flex rounded-md border">
        {(["bgp", "vrrp"] as const).map((k) => (
          <button
            key={k}
            type="button"
            role="radio"
            aria-checked={kind === k}
            onClick={() => setKind(k)}
            className={cn(
              "px-4 py-1.5 text-sm first:rounded-l-md last:rounded-r-md",
              kind === k ? "bg-accent font-medium" : "text-muted-foreground",
            )}
          >
            {k.toUpperCase()}
          </button>
        ))}
      </div>
      {input("name", t("Name"))}
      {kind === "bgp" ? (
        <>
          {input("asn", t("Local ASN"))}
          {input("peer_ip", t("First peer IP"), true)}
          {input("remote_asn", t("Remote ASN"))}
        </>
      ) : (
        <>
          {input("vrid", t("VRID"))}
          {input("interface", t("Interface"), true)}
        </>
      )}
    </Sheet>
  );
}

export function RoutingProfiles({ tenant }: { tenant: string }) {
  const { draft } = useDraft(tenant);
  const list = useProfiles(tenant, draft?.id);
  const groups = useGroups(tenant);
  const can = useCan(tenant);
  const write = can("write", "application");
  const [picked, setPicked] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const items = (list.data?.items ?? []) as Profile[];
  const profile = items.find((p) => p.id === picked) ?? items[0];
  const usedBy = (p: Profile) =>
    (groups.data?.items ?? [])
      .filter((g) => g.routing_profile_id === p.id)
      .map((g) => ({ id: g.id, name: g.name }));
  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("Routing profiles")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("How an SE group announces its VIPs: BGP peering or VRRP")}
          </p>
        </div>
        <Tip label={write.reason}>
          <Button disabled={!write.allowed} onClick={() => setCreating(true)}>
            {t("New routing profile")}
          </Button>
        </Tip>
      </div>
      {list.isPending ? (
        <TableSkeleton cols={2} />
      ) : list.isError ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title={t("No routing profiles yet")}
          body={t("A profile tells an SE group how to announce its VIPs.")}
        />
      ) : (
        <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
          <ul className="space-y-1 rounded-lg border bg-card p-2">
            {items.map((p) => (
              <li key={p.id}>
                <button
                  type="button"
                  aria-current={profile?.id === p.id}
                  onClick={() => setPicked(p.id)}
                  className={cn(
                    "w-full rounded-md px-3 py-2 text-left hover:bg-accent/50",
                    profile?.id === p.id && "bg-accent",
                  )}
                >
                  <span className="flex items-center gap-2 font-medium">
                    {p.draft_status !== "unchanged" && <DraftMarker status={p.draft_status} />}
                    <span className={cn(p.draft_status === "deleted" && "line-through")}>
                      {p.name}
                    </span>
                  </span>
                  <span className="block text-xs text-muted-foreground">
                    {profileLine(
                      p,
                      usedBy(p).map((g) => g.name),
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {profile && (
            <Editor
              key={`${profile.id}-${profile.version}-${profile.draft_status}`}
              tenant={tenant}
              profile={profile}
              usedBy={usedBy(profile)}
            />
          )}
        </div>
      )}
      {creating && <NewProfile tenant={tenant} onClose={() => setCreating(false)} />}
    </div>
  );
}
