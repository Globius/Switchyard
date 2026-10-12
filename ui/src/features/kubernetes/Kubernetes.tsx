/**
 * Kubernetes clusters (tenant-admin): Services of type LoadBalancer and Gateways of a connected
 * cluster become virtual services through a `system:k8s` change set (ADR 0146 and the Kubernetes
 * connector). The service-account token is write-only; saved live and audited.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useMe } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { WriteOnlySecret } from "@/components/sy/WriteOnlySecret";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { shortWhen } from "@/lib/format";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Cluster = components["schemas"]["KubernetesClusterRead"];
type TestRead = components["schemas"]["KubernetesTestRead"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

function useAdmin(tenant: string) {
  const me = useMe().data;
  const role = me?.tenants.find((x) => x.name === tenant)?.role;
  const ok = !!me?.is_system || role === "tenant-admin" || role === "system-admin";
  return { ok, reason: ok ? undefined : t("Needs tenant-admin") };
}

/** One line for the Last sync column. */
export function syncSummary(
  c: Pick<Cluster, "last_sync_ok" | "last_sync" | "last_sync_error" | "last_sync_at">,
): string {
  if (c.last_sync_ok === null || c.last_sync_at === null) return t("Not synced yet");
  if (!c.last_sync_ok) return c.last_sync_error ?? t("Failed");
  const s = c.last_sync;
  if (!s) return t("In sync");
  if (s.skipped) return s.skipped;
  return t("{c} created · {u} updated · {d} deleted", {
    c: s.created ?? 0,
    u: s.updated ?? 0,
    d: s.deleted ?? 0,
  });
}

function ClusterSheet({
  tenant,
  cluster,
  onClose,
}: {
  tenant: string;
  cluster: Cluster | null;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = cluster !== null;
  const groups = useQuery({
    queryKey: ["t", tenant, "se-groups"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/se-groups", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
  const pools = useQuery({
    queryKey: ["t", tenant, "vip-pools"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/vip-pools", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
  const [name, setName] = useState(cluster?.name ?? "");
  const [apiUrl, setApiUrl] = useState(cluster?.api_url ?? "");
  const [token, setToken] = useState("");
  const [ca, setCa] = useState("");
  const [mode, setMode] = useState<"nodeport" | "pods">(cluster?.mode ?? "nodeport");
  const [seGroup, setSeGroup] = useState(cluster?.se_group ?? "");
  const [vipPool, setVipPool] = useState(cluster?.vip_pool ?? "");
  const [className, setClassName] = useState(cluster?.class_name ?? "switchyard");
  const [gatewayClass, setGatewayClass] = useState(cluster?.gateway_class ?? "switchyard");
  const [enabled, setEnabled] = useState(cluster?.enabled ?? true);
  const [problem, setProblem] = useState<unknown>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [test, setTest] = useState<TestRead | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const fail = (e: unknown) => {
    if (e instanceof ApiError && e.status === 422) {
      const m: Record<string, string> = {};
      for (const x of e.problem?.errors ?? []) m[x.path.split(".")[0] ?? x.path] = x.message;
      setErrors(m);
    } else setProblem(e);
  };
  const done = () => {
    void qc.invalidateQueries({ queryKey: ["t", tenant, "kubernetes"] });
    onClose();
  };
  const save = useMutation({
    mutationFn: async () => {
      setProblem(null);
      setErrors({});
      if (!cluster)
        return unwrap(
          await api.POST("/api/v1/tenants/{t}/kubernetes-clusters", {
            params: { path: { t: tenant } },
            body: {
              name,
              api_url: apiUrl,
              token,
              ca_pem: ca || null,
              mode,
              se_group: seGroup,
              vip_pool: vipPool,
              class_name: className,
              gateway_class: gatewayClass,
              enabled,
            },
          }),
        );
      return unwrap(
        await api.PATCH("/api/v1/tenants/{t}/kubernetes-clusters/{cluster}", {
          params: {
            path: { t: tenant, cluster: cluster.id },
            header: { "if-match": String(cluster.version) },
          },
          body: {
            api_url: apiUrl,
            mode,
            se_group: seGroup,
            vip_pool: vipPool,
            class_name: className,
            gateway_class: gatewayClass,
            enabled,
            ...(token ? { token } : {}),
            ...(ca ? { ca_pem: ca } : {}),
          },
        }),
      );
    },
    onSuccess: done,
    onError: fail,
  });
  const runTest = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/kubernetes-clusters/{cluster}/test", {
          params: { path: { t: tenant, cluster: cluster!.id } },
        }),
      ),
    onSuccess: setTest,
    onError: setProblem,
  });
  const remove = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.DELETE("/api/v1/tenants/{t}/kubernetes-clusters/{cluster}", {
          params: {
            path: { t: tenant, cluster: cluster!.id },
            header: { "if-match": String(cluster!.version) },
          },
        }),
      ),
    onSuccess: done,
    onError: (e) => {
      setConfirmDelete(false);
      setProblem(e);
    },
  });
  const valid =
    name !== "" && apiUrl !== "" && seGroup !== "" && vipPool !== "" && (editing || token !== "");
  const err = (k: string) =>
    errors[k] && (
      <span role="alert" className="block text-xs font-normal text-status-down">
        {errors[k]}
      </span>
    );

  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={editing ? t("Edit {name}", { name: cluster.name }) : t("Connect a cluster")}
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
            {editing ? t("Save") : t("Connect")}
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
          disabled={editing}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        {err("name")}
      </label>
      <label className="block space-y-1.5 text-sm font-medium">
        {t("API server URL")}
        <input
          aria-label={t("API server URL")}
          className={cn(INPUT, "font-mono")}
          placeholder="https://k8s.example.test:6443"
          value={apiUrl}
          onChange={(e) => setApiUrl(e.target.value)}
        />
        {err("api_url")}
      </label>
      <WriteOnlySecret
        label={t("Service-account token")}
        isSet={editing}
        value={token}
        onChange={setToken}
        required
        help={t("Needs the access described in docs/kubernetes.md. Stored in OpenBao.")}
      />
      {err("token")}
      <label className="block space-y-1.5 text-sm font-medium">
        {t("API server CA (optional)")}
        <textarea
          aria-label={t("API server CA")}
          className={cn(INPUT, "h-20 py-2 font-mono text-xs")}
          placeholder={
            editing && cluster.ca_set
              ? t("Set. Paste PEM to replace it.")
              : "-----BEGIN CERTIFICATE-----"
          }
          value={ca}
          onChange={(e) => setCa(e.target.value)}
        />
      </label>
      <fieldset className="space-y-1.5">
        <legend className="text-sm font-medium">{t("Pools hold")}</legend>
        <div className="grid grid-cols-2 gap-2">
          {(
            [
              ["nodeport", "Ready nodes", "At the NodePort. Works from any SE network."],
              ["pods", "Ready pods", "Endpoints directly. The SEs must reach the pod network."],
            ] as const
          ).map(([k, label, help]) => (
            <label
              key={k}
              className={cn(
                "flex cursor-pointer items-start gap-2 rounded-lg border bg-card p-2.5 text-sm",
                mode === k && "border-primary bg-primary/10",
              )}
            >
              <input
                type="radio"
                name="k8s-mode"
                aria-label={label}
                className="mt-1"
                checked={mode === k}
                onChange={() => setMode(k)}
              />
              <span>
                <span className="font-medium">{t(label)}</span>
                <span className="block text-xs text-muted-foreground">{t(help)}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1.5">
          <div className="text-sm font-medium">{t("SE group")}</div>
          <Combobox
            aria-label={t("SE group")}
            value={seGroup}
            onChange={setSeGroup}
            placeholder={t("Choose an SE group")}
            options={(groups.data?.items ?? []).map((g) => ({ value: g.name, label: g.name }))}
          />
          {err("se_group")}
        </div>
        <div className="space-y-1.5">
          <div className="text-sm font-medium">{t("VIP pool")}</div>
          <Combobox
            aria-label={t("VIP pool")}
            value={vipPool}
            onChange={setVipPool}
            placeholder={t("Choose a VIP pool")}
            options={(pools.data?.items ?? []).map((p) => ({ value: p.name, label: p.name }))}
          />
          {err("vip_pool")}
        </div>
        <label className="space-y-1.5 text-sm font-medium">
          {t("loadBalancerClass")}
          <input
            aria-label={t("loadBalancerClass")}
            className={cn(INPUT, "font-mono")}
            value={className}
            onChange={(e) => setClassName(e.target.value)}
          />
        </label>
        <label className="space-y-1.5 text-sm font-medium">
          {t("gatewayClassName")}
          <input
            aria-label={t("gatewayClassName")}
            className={cn(INPUT, "font-mono")}
            value={gatewayClass}
            onChange={(e) => setGatewayClass(e.target.value)}
          />
        </label>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <Switch aria-label={t("Enabled")} checked={enabled} onCheckedChange={setEnabled} />
        {t("Enabled")}
      </label>
      {editing && (
        <div className="space-y-2">
          <Button variant="outline" disabled={runTest.isPending} onClick={() => runTest.mutate()}>
            {t("Test connection")}
          </Button>
          {test && (
            <div
              role="status"
              aria-label={t("Test result")}
              className="space-y-1 rounded-lg border p-3 text-sm"
            >
              <div className="font-medium">{test.ok ? t("Connected") : t("Not connected")}</div>
              <p className="text-xs">{test.detail}</p>
              <p className="text-xs text-muted-foreground">
                {[
                  test.version && `Kubernetes ${test.version}`,
                  test.nodes != null && t("{n} nodes", { n: test.nodes }),
                  test.services != null && t("{n} services", { n: test.services }),
                  test.gateway_api != null &&
                    (test.gateway_api ? t("Gateway API installed") : t("No Gateway API")),
                ]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
              {test.problems.map((p) => (
                <p key={p} role="alert" className="text-xs text-status-down">
                  {p}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Disconnect {name}?", { name: cluster?.name ?? "" })}
        confirmLabel={t("Disconnect")}
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
      >
        <p>
          {t(
            "The cluster and its stored token are removed. Virtual services it created stay until a change set removes them. This is audited.",
          )}
        </p>
      </ConfirmDialog>
    </Sheet>
  );
}

export function Kubernetes({ tenant }: { tenant: string }) {
  const admin = useAdmin(tenant);
  const qc = useQueryClient();
  const list = useQuery({
    queryKey: ["t", tenant, "kubernetes", "clusters"],
    refetchInterval: 15_000,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/kubernetes-clusters", {
          params: { path: { t: tenant } },
        }),
      ),
  });
  const [sheet, setSheet] = useState<Cluster | "new" | null>(null);
  const sync = useMutation({
    mutationFn: async (name: string) =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/kubernetes-clusters/{cluster}/sync", {
          params: { path: { t: tenant, cluster: name } },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["t", tenant, "kubernetes"] }),
  });
  const items = list.data ?? [];
  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("Kubernetes")}</h1>
          <p className="text-sm text-muted-foreground">
            {t(
              "Services of type LoadBalancer and Gateways of connected clusters become virtual services through a system:k8s change set. The platform syncs every 30 seconds.",
            )}
          </p>
        </div>
        <Tip label={admin.reason}>
          <Button disabled={!admin.ok} onClick={() => setSheet("new")}>
            {t("Connect a cluster")}
          </Button>
        </Tip>
      </div>
      {sync.error && <ErrorState error={sync.error} />}
      <div className="overflow-x-auto rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={6} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState
            title={t("No clusters connected")}
            body={t("Connect a cluster to publish its LoadBalancer Services and Gateways.")}
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {[
                  "Name",
                  "API server",
                  "Pools hold",
                  "SE group · VIP pool",
                  "Last sync",
                  "Status",
                  "",
                ].map((h, i) => (
                  <th key={i} className="px-4 py-2.5 font-medium">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {items.map((c) => (
                <tr key={c.id}>
                  <td className="px-4 py-3">
                    <button
                      type="button"
                      className="font-medium text-primary hover:underline"
                      onClick={() => setSheet(c)}
                    >
                      {c.name}
                    </button>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs break-all">{c.api_url}</td>
                  <td className="px-4 py-3">
                    {c.mode === "pods" ? t("Ready pods") : t("Ready nodes")}
                  </td>
                  <td className="px-4 py-3">
                    {c.se_group} · {c.vip_pool}
                  </td>
                  <td className="px-4 py-3">
                    <span className="block">{syncSummary(c)}</span>
                    {c.last_sync_at && (
                      <span className="block text-xs text-muted-foreground">
                        {shortWhen(c.last_sync_at)}
                      </span>
                    )}
                    {c.waiting_for_change_set && (
                      <span className="block text-xs text-muted-foreground">
                        {t("waiting for a change set to finish")}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {!c.enabled ? (
                      <StatusPill status="neutral">{t("Disabled")}</StatusPill>
                    ) : c.last_sync_ok === false ? (
                      <StatusPill status="down">{t("Failing")}</StatusPill>
                    ) : c.last_sync_ok ? (
                      <StatusPill status="healthy">{t("Syncing")}</StatusPill>
                    ) : (
                      <StatusPill status="neutral">{t("New")}</StatusPill>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <Tip label={admin.reason}>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={!admin.ok || sync.isPending}
                        onClick={() => sync.mutate(c.id)}
                      >
                        {t("Sync now")}
                      </Button>
                    </Tip>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {sheet !== null && (
        <ClusterSheet
          key={sheet === "new" ? "new" : sheet.id}
          tenant={tenant}
          cluster={sheet === "new" ? null : sheet}
          onClose={() => setSheet(null)}
        />
      )}
    </div>
  );
}
