/** SE groups: engines with live state and actions, Proxmox deploy, and the group's settings. */
import { Combobox } from "@/components/ui/combobox";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { DropdownMenu } from "radix-ui";
import { useMemo, useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { ChipInput } from "@/components/sy/ChipInput";
import { ShowOnceSecret } from "@/components/sy/ShowOnceSecret";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { DraftMarker, StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Menu, MenuContent, MenuTrigger } from "@/components/ui/popover-menu";
import { Sheet } from "@/components/ui/sheet";
import { Tip } from "@/components/ui/tooltip";
import { ReapplyButton } from "@/features/drift/ReapplyButton";
import { RunningConfigButton } from "@/features/drift/RunningConfig";
import { useSaveOp } from "@/features/objects/save";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Group = components["schemas"]["SeGroupRead"];
type Engine = components["schemas"]["EngineRead"];
type EngineStatus = components["schemas"]["EngineStatusRead"];

/** "VM 201 · pve-atl-01 · " from the engine read; nothing for a hand-built engine without a guest type. */
function guestPrefix(e: Engine): string {
  if (!e.guest_kind) return "";
  const kind = e.guest_kind === "lxc" ? "CT" : "VM";
  return (
    [e.vmid != null ? `${kind} ${e.vmid}` : kind, e.pve_node].filter(Boolean).join(" · ") + " · "
  );
}

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

function useGroups(tenant: string, changeSet?: string) {
  return useQuery({
    queryKey: ["t", tenant, "se-groups", { changeSet }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/se-groups", {
          params: { path: { t: tenant }, query: { limit: 200, change_set: changeSet } },
        }),
      ),
  });
}

function useEngines(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "engines"],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/engines", { params: { path: { t: tenant } } })),
  });
}

function useEngineStatus(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "status", "engines"],
    refetchInterval: 10_000,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/status/engines", { params: { path: { t: tenant } } }),
      ),
  });
}

function useRoutingProfiles(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "routing-profiles"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/routing-profiles", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
}

function useNetworks(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "networks"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/networks", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
}

function useVipPools(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "vip-pools"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/vip-pools", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
}

function Live() {
  return <StatusPill status="pending">{t("Live")}</StatusPill>;
}

/** An engine action: runs now, outside the change set. */
function useEngineAction(tenant: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (a: {
      kind: "state" | "retire" | "delete" | "token";
      engine: string;
      state?: "enabled" | "disabled";
    }) => {
      const path = { t: tenant, engine: a.engine };
      switch (a.kind) {
        case "state":
          return unwrap(
            await api.PUT("/api/v1/tenants/{t}/engines/{engine}/state", {
              params: { path, query: { state: a.state! } },
            }),
          );
        case "retire":
          return unwrap(
            await api.POST("/api/v1/tenants/{t}/engines/{engine}/retire", { params: { path } }),
          );
        case "delete":
          return unwrap(
            await api.DELETE("/api/v1/tenants/{t}/engines/{engine}", { params: { path } }),
          );
        case "token":
          return unwrap(
            await api.POST("/api/v1/tenants/{t}/engines/{engine}/enrollment-token", {
              params: { path },
            }),
          );
      }
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["t", tenant] }),
  });
}

function RowMenu({
  label,
  items,
}: {
  label: string;
  items: { label: string; disabled?: boolean; onSelect: () => void }[];
}) {
  return (
    <Menu>
      <MenuTrigger asChild>
        <Button size="sm" variant="ghost">
          {label}
        </Button>
      </MenuTrigger>
      <MenuContent className="w-56">
        {items.map((i) => (
          <DropdownMenu.Item
            key={i.label}
            disabled={i.disabled}
            onSelect={i.onSelect}
            className="cursor-default rounded-md px-3 py-2 text-sm outline-none data-[disabled]:opacity-50 data-[highlighted]:bg-accent"
          >
            {i.label}
          </DropdownMenu.Item>
        ))}
      </MenuContent>
    </Menu>
  );
}

function ConfigPill({ st }: { st: EngineStatus | undefined }) {
  if (!st || !st.reachable) return <span className="text-muted-foreground">—</span>;
  return st.drift.length > 0 ? (
    <StatusPill status="degraded">{t("Drifted")}</StatusPill>
  ) : (
    <StatusPill status="healthy">{t("In sync")}</StatusPill>
  );
}

function EnginesCard({
  tenant,
  group,
  onDeploy,
  onRegister,
}: {
  tenant: string;
  group: Group;
  onDeploy: () => void;
  onRegister: () => void;
}) {
  const engines = useEngines(tenant);
  const status = useEngineStatus(tenant);
  const can = useCan(tenant);
  const manage = can("manage", "engine");
  const act = useEngineAction(tenant);
  const [confirm, setConfirm] = useState<{ kind: "retire" | "delete"; engine: Engine } | null>(
    null,
  );
  const [tokenStored, setTokenStored] = useState(false);
  const [token, setToken] = useState<{ name: string; token: string; expires_at: string } | null>(
    null,
  );
  const rows = (engines.data ?? []).filter((e) => e.se_group_id === group.id);
  const stOf = (id: string) => status.data?.find((s) => s.engine_id === id);
  const drifted = rows.filter((e) => (stOf(e.id)?.drift.length ?? 0) > 0);
  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <header className="flex flex-wrap items-center gap-3 border-b px-4 py-3">
        <h2 className="text-lg font-semibold">{t("Engines")}</h2>
        <Live />
        <span className="text-xs text-muted-foreground">
          {t("Engine actions act immediately and need tenant-admin")}
        </span>
        <div className="ml-auto flex gap-2">
          <Tip label={manage.reason}>
            <Button variant="outline" disabled={!manage.allowed} onClick={onRegister}>
              {t("Register existing engine")}
            </Button>
          </Tip>
          <Tip label={manage.reason}>
            <Button disabled={!manage.allowed} onClick={onDeploy}>
              {t("Deploy engines")}
            </Button>
          </Tip>
        </div>
      </header>
      {engines.isPending ? (
        <TableSkeleton cols={6} />
      ) : engines.isError ? (
        <ErrorState error={engines.error} onRetry={() => void engines.refetch()} />
      ) : rows.length === 0 ? (
        <EmptyState
          title={t("No engines in this group")}
          body={t("Deploy engines on Proxmox, or register one you already run.")}
        />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-[13px]">
            <thead className="text-left text-xs text-muted-foreground">
              <tr>
                {["Engine", "State", "Reachable", "BGP sessions", "Config", "Observed", ""].map(
                  (h, i) => (
                    <th key={i} className="px-4 py-2.5 font-medium">
                      {t(h)}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((e) => {
                const st = stOf(e.id);
                const next = e.state === "enabled" ? "disabled" : "enabled";
                return (
                  <tr key={e.id}>
                    <td className="px-4 py-3">
                      <div className="font-mono text-sm">{e.name}</div>
                      <div className="font-mono text-xs text-muted-foreground">
                        {guestPrefix(e)}
                        {e.mgmt_ip ?? "—"}
                        {st?.agent?.version ? ` · agent ${st.agent.version}` : ""}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      {e.state === "enabled" ? t("Enabled") : t("Disabled")}
                    </td>
                    <td className="px-4 py-3">
                      {st ? (
                        st.reachable ? (
                          <StatusPill status="healthy">{t("Yes")}</StatusPill>
                        ) : (
                          <StatusPill status="down">{t("No")}</StatusPill>
                        )
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-col gap-1">
                        {(st?.agent?.bgp ?? []).map((b) => (
                          <span
                            key={b.peer}
                            className={cn(
                              "w-fit rounded px-1.5 py-0.5 font-mono text-xs",
                              b.state.toLowerCase().startsWith("estab")
                                ? "bg-status-healthy/15 text-status-healthy"
                                : "bg-status-down/15 text-status-down",
                            )}
                          >
                            {b.peer} {b.state}
                          </span>
                        ))}
                        {(st?.agent?.bgp.length ?? 0) === 0 && "—"}
                      </div>
                    </td>
                    <td className="px-4 py-3">
                      <ConfigPill st={st} />
                    </td>
                    <td className="px-4 py-3 text-muted-foreground">
                      {st ? new Date(st.observed_at).toLocaleTimeString() : "—"}
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex items-center justify-end gap-2">
                        <Tip label={manage.reason}>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={!manage.allowed || act.isPending}
                            onClick={() => act.mutate({ kind: "state", engine: e.id, state: next })}
                          >
                            {e.state === "enabled" ? t("Disable") : t("Enable")}
                          </Button>
                        </Tip>
                        <RowMenu
                          label={t("More")}
                          items={[
                            {
                              label: t("Enrollment token"),
                              disabled: !manage.allowed,
                              onSelect: async () => {
                                const r = await act.mutateAsync({ kind: "token", engine: e.id });
                                act.reset();
                                setTokenStored(false);
                                setToken({
                                  name: e.name,
                                  token: (r as { token: string }).token,
                                  expires_at: (r as { expires_at: string }).expires_at,
                                });
                              },
                            },
                            {
                              label: t("Retire and destroy VM"),
                              disabled: !manage.allowed || e.vmid == null,
                              onSelect: () => setConfirm({ kind: "retire", engine: e }),
                            },
                            {
                              label: t("Remove record"),
                              disabled: !manage.allowed,
                              onSelect: () => setConfirm({ kind: "delete", engine: e }),
                            },
                          ]}
                        />
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {drifted.map((e) => (
        <div
          key={e.id}
          className="m-4 rounded-md border border-status-degraded/40 bg-status-degraded/10 p-3 text-sm"
        >
          <div className="font-medium">
            {e.name} {t("is drifted")}
          </div>
          <ul className="mt-1 font-mono text-xs text-muted-foreground">
            {stOf(e.id)?.drift.map((d, i) => (
              <li key={i}>
                {d.kind}
                {d.path ? ` · ${d.path}` : ""}
              </li>
            ))}
          </ul>
          <p className="mt-1 mb-2 text-xs text-muted-foreground">
            {t("Reported only; nothing is changed automatically.")}
          </p>
          <span className="flex flex-wrap items-center gap-2">
            <ReapplyButton tenant={tenant} groupId={group.id} engine={e.name} />
            <RunningConfigButton tenant={tenant} engine={{ id: e.id, name: e.name }} />
          </span>
        </div>
      ))}
      {act.error && <ErrorState error={act.error} />}
      <p className="border-t px-4 py-3 text-xs text-muted-foreground">
        {t(
          "More holds the enrollment token for the agent, retire (destroys the VM or container and its record) and remove record (deletes only the record; the guest keeps running). The line under each name shows its guest type (VM or CT), Proxmox ID and node. If an engine's VIP routes are withdrawn, the other engines keep serving.",
        )}
      </p>
      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={
          confirm?.kind === "retire"
            ? t("Retire {name}?", { name: confirm.engine.name })
            : t("Remove {name}?", { name: confirm?.engine.name ?? "" })
        }
        confirmLabel={confirm?.kind === "retire" ? t("Retire engine") : t("Remove record")}
        busy={act.isPending}
        onConfirm={async () => {
          if (!confirm) return;
          try {
            await act.mutateAsync({ kind: confirm.kind, engine: confirm.engine.id });
          } finally {
            setConfirm(null);
          }
        }}
      >
        <p>
          {confirm?.kind === "retire"
            ? t(
                "Runs as a job: the engine is drained and its guest is destroyed on Proxmox. This acts immediately, outside the change set.",
              )
            : t(
                "Only the controller's record is removed; the guest keeps running. This acts immediately, outside the change set.",
              )}
        </p>
      </ConfirmDialog>
      {token && (
        <Sheet
          open
          centered
          onOpenChange={(o) => !o && tokenStored && setToken(null)}
          title={t("Enrollment token for {name}", { name: token.name })}
          footer={
            <Button className="ml-auto" disabled={!tokenStored} onClick={() => setToken(null)}>
              {t("Done")}
            </Button>
          }
        >
          <ShowOnceSecret
            secrets={[{ label: t("Enrollment token"), value: token.token }]}
            stored={tokenStored}
            onStoredChange={setTokenStored}
            note={t("Shown once. Give it to the engine's agent; it expires {when}.", {
              when: new Date(token.expires_at).toLocaleString(),
            })}
          />
        </Sheet>
      )}
    </section>
  );
}

function DeploySheet({
  tenant,
  group,
  current,
  onClose,
}: {
  tenant: string;
  group: Group;
  current: number;
  onClose: () => void;
}) {
  const pools = useVipPools(tenant);
  const qc = useQueryClient();
  const [total, setTotal] = useState(current + 1);
  const [guest, setGuest] = useState<"qemu" | "lxc">("qemu");
  const [ack, setAck] = useState(false);
  const [template, setTemplate] = useState("");
  const [nodes, setNodes] = useState<string[]>([]);
  const [pool, setPool] = useState("");
  const [mgmt, setMgmt] = useState("");
  const [cred, setCred] = useState("");
  const [keys, setKeys] = useState("");
  const [problem, setProblem] = useState<unknown>(null);
  const [done, setDone] = useState(false);
  const run = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/se-groups/{group}/deploy", {
          params: { path: { t: tenant, group: group.id } },
          body: {
            count: total - current,
            guest_kind: guest,
            template_vmid: Number(template),
            nodes,
            mgmt_pool_id: mgmt,
            pool: pool || null,
            credentials_ref: cred || null,
            ssh_public_keys: keys || null,
          },
        }),
      ),
    onSuccess: () => {
      setDone(true);
      void qc.invalidateQueries({ queryKey: ["t", tenant] });
    },
    onError: setProblem,
  });
  const lxc = guest === "lxc";
  const valid =
    total > current && Number(template) > 0 && nodes.length > 0 && mgmt !== "" && (!lxc || ack);
  return (
    <Sheet
      open
      centered
      onOpenChange={(o) => !o && onClose()}
      title={t("Deploy engines for {name}", { name: group.name })}
      badge={<Live />}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {done ? t("Close") : t("Cancel")}
          </Button>
          {!done && lxc && !ack && (
            <span id="deploy-why" className="text-xs text-muted-foreground">
              {t("Confirm the container risks above to deploy.")}
            </span>
          )}
          {!done && (
            <Button
              disabled={!valid || run.isPending}
              aria-describedby={lxc && !ack ? "deploy-why" : undefined}
              onClick={() => run.mutate()}
            >
              {t("Start deploy")}
            </Button>
          )}
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      {done ? (
        <p role="status" className="text-sm">
          {t("Deploying as a job. Follow it on the Jobs screen.")}
        </p>
      ) : (
        <>
          <p className="text-sm text-muted-foreground">
            {t(
              "Clones engines from a template on Proxmox and registers them. Runs as a job; if a step fails, the guests it created are removed and existing engines are untouched.",
            )}
          </p>
          <fieldset className="space-y-1.5">
            <legend className="mb-1.5 text-sm font-medium">{t("Guest type")}</legend>
            <div role="radiogroup" className="grid grid-cols-2 gap-2">
              {(
                [
                  [
                    "qemu",
                    "Virtual machine",
                    "Own kernel. The tested option for HTTP, TCP, UDP, VRRP failover and BGP.",
                  ],
                  [
                    "lxc",
                    "Container (LXC)",
                    "Lighter and faster to start. Privileged, and VRRP failover and BGP are untested.",
                  ],
                ] as const
              ).map(([k, label, text]) => (
                <label
                  key={k}
                  className={cn(
                    "flex cursor-pointer items-start gap-2.5 rounded-lg border bg-card px-3 py-2.5",
                    guest === k && "border-primary ring-1 ring-primary",
                  )}
                >
                  <input
                    type="radio"
                    name="guest-kind"
                    value={k}
                    checked={guest === k}
                    className="mt-1"
                    onChange={() => {
                      setGuest(k);
                      setAck(false);
                    }}
                  />
                  <span className="flex flex-col gap-0.5 text-sm">
                    <span className="font-medium">
                      {t(label)}
                      {k === "qemu" && (
                        <span className="font-normal text-muted-foreground">
                          {" "}
                          {t("(recommended)")}
                        </span>
                      )}
                    </span>
                    <span className="text-xs text-muted-foreground">{t(text)}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
          {lxc && (
            <div
              role="note"
              className="space-y-2 rounded-lg border border-status-degraded/40 bg-status-degraded-bg p-3 text-xs"
            >
              <strong className="block text-sm font-semibold">
                {t("Containers are privileged and partly untested")}
              </strong>
              <ul className="list-disc space-y-0.5 pl-4">
                <li>
                  {t(
                    "Engines run as privileged containers with nesting on. Root in the container is root on the Proxmox host. Unprivileged containers aren't supported.",
                  )}
                </li>
                <li>
                  {t(
                    "VRRP has only been tested with one engine, so failover between engines is untested.",
                  )}
                </li>
                <li>{t("BGP is untested.")}</li>
                <li>
                  {t(
                    "UDP services need the ip_vs modules loaded on each Proxmox host in the list.",
                  )}
                </li>
                <li>
                  {t(
                    "No cloud-init: enrollment isn't automatic. After the job, open each container's console and run sy-agent enroll with its token.",
                  )}
                </li>
              </ul>
              <label className="flex items-start gap-2 pt-1">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={ack}
                  onChange={(e) => setAck(e.target.checked)}
                />
                <span>
                  {t(
                    "I understand the risks of a privileged container and that failover and BGP are untested.",
                  )}
                </span>
              </label>
            </div>
          )}
          <div className="grid grid-cols-2 gap-3">
            <label className="space-y-1.5 text-sm font-medium">
              {t("Engines in the group")}
              <input
                aria-label={t("Engines in the group")}
                type="number"
                min={current + 1}
                className={INPUT}
                value={total}
                onChange={(e) => setTotal(Number(e.target.value))}
              />
              <span className="block text-xs font-normal text-muted-foreground">
                {t("Total wanted. Today: {n}. Adds {add}.", {
                  n: current,
                  add: Math.max(total - current, 0),
                })}
              </span>
            </label>
            <label className="space-y-1.5 text-sm font-medium">
              {lxc ? t("Container template") : t("Template VM ID")}
              <input
                aria-label={lxc ? t("Container template") : t("Template VM ID")}
                type="number"
                className={INPUT}
                value={template}
                onChange={(e) => setTemplate(e.target.value)}
              />
            </label>
          </div>
          <div className="space-y-1.5">
            <div className="text-sm font-medium">{t("Proxmox nodes")}</div>
            <ChipInput
              label={t("Proxmox nodes")}
              value={nodes}
              onChange={setNodes}
              freeText={{
                validate: (x) => (/^[a-z0-9][a-z0-9.-]*$/i.test(x) ? null : t("Use a node name.")),
              }}
              placeholder="pve-01"
            />
            <span className="block text-xs text-muted-foreground">
              {t("New engines are spread round-robin over these.")}
            </span>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <label className="space-y-1.5 text-sm font-medium">
              {t("Proxmox pool")}
              <input
                aria-label={t("Proxmox pool")}
                className={INPUT}
                value={pool}
                onChange={(e) => setPool(e.target.value)}
              />
            </label>
            <label className="space-y-1.5 text-sm font-medium">
              {t("Management addresses from")}
              <select
                aria-label={t("Management addresses from")}
                className={INPUT}
                value={mgmt}
                onChange={(e) => setMgmt(e.target.value)}
              >
                <option value="">{t("Choose…")}</option>
                {pools.data?.items.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Data Plane API credentials reference")}
            <input
              aria-label={t("Data Plane API credentials reference")}
              className={cn(INPUT, "font-mono")}
              placeholder="openbao://kv/switchyard/…"
              value={cred}
              onChange={(e) => setCred(e.target.value)}
            />
          </label>
          <label className="block space-y-1.5 text-sm font-medium">
            {t("SSH public keys for the engines")}
            <textarea
              aria-label={t("SSH public keys for the engines")}
              className={cn(INPUT, "h-20 py-2 font-mono text-xs")}
              value={keys}
              onChange={(e) => setKeys(e.target.value)}
            />
            <span className="block text-xs font-normal text-muted-foreground">
              {t("For people to log in to the engine. The controller itself never uses SSH.")}
            </span>
          </label>
          <p className="rounded-md bg-muted p-3 text-sm">
            {t(
              "New engines join disabled and need an enrollment token for their agent. Create one per engine from its row once the job finishes.",
            )}
          </p>
        </>
      )}
    </Sheet>
  );
}

function RegisterSheet({
  tenant,
  group,
  onClose,
}: {
  tenant: string;
  group: Group;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const [f, setF] = useState({ name: "", dataplane_url: "", mgmt_ip: "", credentials_ref: "" });
  const reg = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/se-groups/{group}/engines", {
          params: { path: { t: tenant, group: group.id } },
          body: {
            name: f.name,
            dataplane_url: f.dataplane_url,
            mgmt_ip: f.mgmt_ip || null,
            credentials_ref: f.credentials_ref || null,
          },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["t", tenant] });
      onClose();
    },
  });
  const field = (k: keyof typeof f, label: string, mono = false) => (
    <label className="block space-y-1.5 text-sm font-medium">
      {label}
      <input
        aria-label={label}
        className={cn(INPUT, mono && "font-mono")}
        value={f[k]}
        onChange={(e) => setF({ ...f, [k]: e.target.value })}
      />
    </label>
  );
  return (
    <Sheet
      open
      centered
      onOpenChange={(o) => !o && onClose()}
      title={t("Register an existing engine in {name}", { name: group.name })}
      badge={<Live />}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            disabled={!f.name || !f.dataplane_url || reg.isPending}
            onClick={() => reg.mutate()}
          >
            {t("Register")}
          </Button>
        </>
      }
    >
      {reg.error && <ErrorState error={reg.error} />}
      {field("name", t("Name"))}
      {field("dataplane_url", t("Data Plane API URL"), true)}
      {field("mgmt_ip", t("Management address"), true)}
      {field("credentials_ref", t("Credentials reference"), true)}
    </Sheet>
  );
}

function usePlacement(tenant: string, group: string) {
  return useQuery({
    queryKey: ["t", tenant, "se-groups", group, "placement"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/se-groups/{group}/placement", {
          params: { path: { t: tenant, group } },
        }),
      ),
  });
}

const GIB = 1024 ** 3;

/** Where the group's engines run on Proxmox, with a warning when one host carries them all. */
function PlacementCard({ tenant, group }: { tenant: string; group: Group }) {
  const q = usePlacement(tenant, group.id);
  const kindOf = new Map(useEngines(tenant).data?.map((e) => [e.id, e.guest_kind]));
  const nodes = q.data?.nodes ?? [];
  const withEngines = nodes.filter((n) => n.engines.length > 0);
  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <header className="flex flex-wrap items-center gap-3 border-b px-4 py-3">
        <h2 className="text-lg font-semibold">{t("Placement on Proxmox")}</h2>
        <Live />
        <span className="text-xs text-muted-foreground">
          {t("all SE groups in {tenant}", { tenant })}
        </span>
      </header>
      {q.isPending ? (
        <TableSkeleton cols={3} />
      ) : q.isError ? (
        <ErrorState error={q.error} onRetry={() => void q.refetch()} />
      ) : !q.data.available ? (
        <p className="p-4 text-sm text-muted-foreground">
          {t("Host state not reported")}
          {q.data.reason ? `: ${q.data.reason}` : ""}
        </p>
      ) : (
        <div className="space-y-3 p-4">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {nodes.map((n) => (
              <div key={n.name} className="space-y-2 rounded-lg border p-3 text-sm">
                <div className="flex items-center gap-2">
                  <span className="font-mono font-medium">{n.name}</span>
                  {n.online === false ? (
                    <StatusPill status="down">{t("Offline")}</StatusPill>
                  ) : (
                    <StatusPill status="healthy">{t("Online")}</StatusPill>
                  )}
                </div>
                <div className="text-xs text-muted-foreground">
                  {n.cpu != null && n.max_cpu
                    ? t("CPU {pct}% of {n}", { pct: Math.round(n.cpu * 100), n: n.max_cpu })
                    : t("CPU not reported")}
                  {" · "}
                  {n.mem_used != null && n.mem_total
                    ? t("memory {used} of {total} GiB", {
                        used: (n.mem_used / GIB).toFixed(0),
                        total: (n.mem_total / GIB).toFixed(0),
                      })
                    : t("memory not reported")}
                </div>
                {n.engines.length === 0 ? (
                  <p className="text-xs text-muted-foreground">{t("No engines of yours")}</p>
                ) : (
                  <ul className="space-y-1">
                    {n.engines.map((e) => (
                      <li key={e.id} className="rounded-md bg-muted px-2 py-1 font-mono text-xs">
                        {e.name}
                        {e.vmid != null
                          ? ` · ${kindOf.get(e.id) === "lxc" ? "CT" : "VM"} ${e.vmid}`
                          : ""}
                        {e.state === "disabled" ? ` · ${t("disabled")}` : ""}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            ))}
          </div>
          {nodes.length > 0 && withEngines.length === 1 && withEngines[0]!.engines.length > 1 && (
            <p
              role="alert"
              className="rounded-md border border-status-degraded/40 bg-status-degraded-bg p-3 text-sm"
            >
              {t(
                "All engines run on {node}. Losing that host takes every engine down; spread them over more hosts.",
                { node: withEngines[0]!.name },
              )}
            </p>
          )}
        </div>
      )}
    </section>
  );
}

function Settings({ tenant, group }: { tenant: string; group: Group }) {
  const profiles = useRoutingProfiles(tenant);
  const networks = useNetworks(tenant);
  const save = useSaveOp(tenant, "se_group");
  const can = useCan(tenant);
  const write = can("write", "application");
  const base = useMemo(
    () => ({
      name: group.name,
      routing_profile_id: group.routing_profile_id ?? "",
      maxconn: group.maxconn,
      canary_seconds: group.canary_seconds,
    }),
    [group],
  );
  const [f, setF] = useState(base);
  const [problem, setProblem] = useState<unknown>(null);
  const patch = Object.fromEntries(
    (Object.keys(f) as (keyof typeof f)[])
      .filter((k) => f[k] !== base[k])
      .map((k) => [k, k === "routing_profile_id" && f[k] === "" ? null : f[k]]),
  );
  const dirty = Object.keys(patch).length > 0;
  const kindHint = t("BGP: every engine advertises the VIPs. VRRP: one engine owns each VIP.");
  return (
    <section className="space-y-4 rounded-lg border bg-card p-4">
      <h2 className="text-lg font-semibold">{t("Settings")}</h2>
      {problem != null && <ErrorState error={problem} />}
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1.5 text-sm font-medium">
          {t("Name")}
          <input
            aria-label={t("Name")}
            className={INPUT}
            disabled={!write.allowed}
            value={f.name}
            onChange={(e) => setF({ ...f, name: e.target.value })}
          />
        </label>
        <div className="space-y-1.5 text-sm font-medium">
          {t("Routing mode")}
          <div className="flex h-9 items-center text-sm font-normal">
            {group.ha_mode.toUpperCase()}
          </div>
        </div>
      </div>
      <p className="text-xs text-muted-foreground">{kindHint}</p>
      <div className="grid gap-3 sm:grid-cols-[1fr_12rem_12rem]">
        <label className="space-y-1.5 text-sm font-medium">
          {t("Routing profile")}
          <Combobox
            aria-label={t("Routing profile")}
            disabled={!write.allowed}
            value={f.routing_profile_id}
            onChange={(v) => setF({ ...f, routing_profile_id: v })}
            options={[
              { value: "", label: t("None") },
              ...(profiles.data?.items ?? []).map((p) => ({ value: p.id, label: p.name })),
            ]}
          />
        </label>
        <label className="space-y-1.5 text-sm font-medium">
          {t("Max connections")}
          <input
            aria-label={t("Max connections")}
            type="number"
            className={INPUT}
            disabled={!write.allowed}
            value={f.maxconn}
            onChange={(e) => setF({ ...f, maxconn: Number(e.target.value) })}
          />
        </label>
        <label className="space-y-1.5 text-sm font-medium">
          {t("Canary time")}
          <input
            aria-label={t("Canary time")}
            type="number"
            className={INPUT}
            disabled={!write.allowed}
            value={f.canary_seconds}
            onChange={(e) => setF({ ...f, canary_seconds: Number(e.target.value) })}
          />
          <span className="block text-xs font-normal text-muted-foreground">{t("seconds")}</span>
        </label>
      </div>
      <div>
        <div className="mb-1 text-sm font-medium">{t("Networks")}</div>
        <table className="w-full overflow-hidden rounded-md border text-[13px]">
          <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
            <tr>
              <th className="px-3 py-2 font-medium">{t("Network")}</th>
              <th className="px-3 py-2 font-medium">{t("Role")}</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {group.networks.map((n) => {
              const net = networks.data?.items.find((x) => x.id === n.network_id);
              return (
                <tr key={`${n.network_id}-${n.role}`}>
                  <td className="px-3 py-2">
                    {net?.name ?? n.network_id.slice(0, 8)}{" "}
                    <span className="font-mono text-xs text-muted-foreground">{net?.cidr}</span>
                  </td>
                  <td className="px-3 py-2">
                    {{ vip: t("VIP"), backend: t("Backend"), mgmt: t("Management") }[n.role]}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="flex justify-end gap-2">
        <Button variant="ghost" disabled={!dirty} onClick={() => setF(base)}>
          {t("Revert")}
        </Button>
        <Tip label={write.reason}>
          <Button
            disabled={!write.allowed || !dirty || save.isPending}
            onClick={async () => {
              setProblem(null);
              try {
                await save.mutateAsync({
                  action: "update",
                  id: group.id,
                  version: group.version,
                  body: patch,
                });
              } catch (e) {
                setProblem(e);
              }
            }}
          >
            {t("Save to draft")}
          </Button>
        </Tip>
      </div>
    </section>
  );
}

function NewGroup({ tenant, onClose }: { tenant: string; onClose: () => void }) {
  const save = useSaveOp(tenant, "se_group");
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"bgp" | "vrrp">("bgp");
  const [problem, setProblem] = useState<unknown>(null);
  return (
    <Sheet
      open
      centered
      onOpenChange={(o) => !o && onClose()}
      title={t("New SE group")}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            disabled={!name || save.isPending}
            onClick={async () => {
              try {
                await save.mutateAsync({
                  action: "create",
                  body: { name, ha_mode: mode, networks: [] },
                });
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
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Name")}
        <input
          aria-label={t("Name")}
          className={INPUT}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Routing mode")}
        <select
          aria-label={t("Routing mode")}
          className={INPUT}
          value={mode}
          onChange={(e) => setMode(e.target.value as "bgp" | "vrrp")}
        >
          <option value="bgp">BGP</option>
          <option value="vrrp">VRRP</option>
        </select>
      </label>
    </Sheet>
  );
}

export function SeGroups({ tenant }: { tenant: string }) {
  const { draft } = useDraft(tenant);
  const groups = useGroups(tenant, draft?.id);
  const engines = useEngines(tenant);
  const status = useEngineStatus(tenant);
  const can = useCan(tenant);
  const write = can("write", "application");
  const [picked, setPicked] = useState<string | null>(null);
  const [sheet, setSheet] = useState<"deploy" | "register" | "new" | null>(null);
  const items = groups.data?.items ?? [];
  const group = items.find((g) => g.id === picked) ?? items[0];
  const summary = (g: Group) => {
    const es = (engines.data ?? []).filter((e) => e.se_group_id === g.id);
    const sts = es.map((e) => status.data?.find((s) => s.engine_id === e.id));
    const drifted = sts.filter((s) => (s?.drift.length ?? 0) > 0).length;
    const reachable = sts.filter((s) => s?.reachable).length;
    if (es.length === 0) return { tone: "neutral" as const, text: t("No engines") };
    return drifted > 0
      ? { tone: "degraded" as const, text: t("{n} drifted", { n: drifted }) }
      : { tone: "healthy" as const, text: t("{n} reachable", { n: reachable }) };
  };
  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <h1 className="text-2xl font-semibold">{t("SE groups")}</h1>
        <Tip label={write.reason}>
          <Button disabled={!write.allowed} onClick={() => setSheet("new")}>
            <Plus /> {t("New SE group")}
          </Button>
        </Tip>
      </div>
      {groups.isPending ? (
        <TableSkeleton cols={3} />
      ) : groups.isError ? (
        <ErrorState error={groups.error} onRetry={() => void groups.refetch()} />
      ) : items.length === 0 ? (
        <EmptyState
          title={t("No SE groups yet")}
          body={t("A group is a set of engines that serve the same virtual services.")}
        />
      ) : (
        <>
          <div role="tablist" className="flex gap-1 overflow-x-auto border-b">
            {items.map((g) => {
              const s = summary(g);
              return (
                <button
                  key={g.id}
                  role="tab"
                  type="button"
                  aria-selected={group?.id === g.id}
                  onClick={() => setPicked(g.id)}
                  className={cn(
                    "-mb-px flex items-center gap-2 border-b-2 border-transparent px-4 py-2 text-sm text-muted-foreground",
                    group?.id === g.id && "border-primary font-medium text-foreground",
                  )}
                >
                  {g.draft_status !== "unchanged" && <DraftMarker status={g.draft_status} />}
                  {g.name}
                  <StatusPill status={s.tone}>{s.text}</StatusPill>
                </button>
              );
            })}
          </div>
          {group && (
            <div className="space-y-5" key={`${group.id}-${group.version}-${group.draft_status}`}>
              <EnginesCard
                tenant={tenant}
                group={group}
                onDeploy={() => setSheet("deploy")}
                onRegister={() => setSheet("register")}
              />
              <PlacementCard tenant={tenant} group={group} />
              <Settings tenant={tenant} group={group} />
              {sheet === "deploy" && (
                <DeploySheet
                  tenant={tenant}
                  group={group}
                  current={(engines.data ?? []).filter((e) => e.se_group_id === group.id).length}
                  onClose={() => setSheet(null)}
                />
              )}
              {sheet === "register" && (
                <RegisterSheet tenant={tenant} group={group} onClose={() => setSheet(null)} />
              )}
            </div>
          )}
        </>
      )}
      {sheet === "new" && <NewGroup tenant={tenant} onClose={() => setSheet(null)} />}
    </div>
  );
}
