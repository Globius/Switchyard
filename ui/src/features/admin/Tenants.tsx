// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Admin: tenants, their quotas and the approval setting. Changes are immediate and audited. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Tenant = components["schemas"]["TenantRead"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

const SHORT: Record<string, string> = {
  virtual_service: "VS",
  service_engine: "SE",
  pool: "pools",
  certificate: "certs",
  virtual_services: "VS",
  service_engines: "SE",
  pools: "pools",
  certificates: "certs",
};

/** `20 VS · 4 SE` for a tenant's quotas. */
export function quotaLine(q: Record<string, number> | undefined): string {
  const e = Object.entries(q ?? {});
  return e.length ? e.map(([k, v]) => `${v} ${SHORT[k] ?? k.replace(/_/g, " ")}`).join(" · ") : "—";
}

function useTenants() {
  return useQuery({
    queryKey: ["admin", "tenants"],
    queryFn: async () => unwrap(await api.GET("/api/v1/tenants")),
  });
}

function Detail({ tenant, onDeleted }: { tenant: Tenant; onDeleted: () => void }) {
  const qc = useQueryClient();
  const base = Object.entries(tenant.quotas ?? {}).map(([k, v]) => ({ key: k, limit: String(v) }));
  const [rows, setRows] = useState(base);
  const [approval, setApproval] = useState(tenant.require_approval);
  const [confirm, setConfirm] = useState(false);
  const quotas = Object.fromEntries(rows.filter((r) => r.key).map((r) => [r.key, Number(r.limit)]));
  const dirty =
    approval !== tenant.require_approval ||
    JSON.stringify(quotas) !== JSON.stringify(tenant.quotas ?? {});
  const save = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.PATCH("/api/v1/tenants/{t}", {
          params: { path: { t: tenant.name }, header: { "if-match": String(tenant.version) } },
          body: { quotas, require_approval: approval },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["admin", "tenants"] }),
  });
  const del = useMutation({
    mutationFn: async () =>
      unwrap(await api.DELETE("/api/v1/tenants/{t}", { params: { path: { t: tenant.name } } })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "tenants"] });
      onDeleted();
    },
  });
  return (
    <aside className="rounded-lg border bg-card" aria-label={t("Tenant details")}>
      <header className="flex items-center gap-2 border-b px-4 py-3">
        <h2 className="text-base font-semibold">{tenant.name}</h2>
        <Badge tone="pending">{t("Live")}</Badge>
      </header>
      <div className="space-y-5 p-4 text-sm">
        <div>
          <div className="font-medium">{t("Who has access")}</div>
          <p className="text-muted-foreground">
            {t("{n} people have access.", { n: tenant.members ?? 0 })}{" "}
            <Link to="/admin/role-mappings" className="text-primary hover:underline">
              {t("Edit role mappings")}
            </Link>
            {" · "}
            <Link to="/admin/users" className="text-primary hover:underline">
              {t("See users")}
            </Link>
          </p>
        </div>
        <section className="space-y-2">
          <div className="font-medium">{t("Quotas")}</div>
          {rows.length > 0 && (
            <table className="w-full overflow-hidden rounded-md border text-[13px]">
              <thead className="text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">{t("Quota")}</th>
                  <th className="px-3 py-2 font-medium">{t("Limit")}</th>
                  <th className="px-3 py-2 font-medium">{t("Used")}</th>
                  <th />
                </tr>
              </thead>
              <tbody className="divide-y">
                {rows.map((r, i) => (
                  <tr key={i}>
                    <td className="px-3 py-2">
                      <input
                        aria-label={t("Quota {n}", { n: i + 1 })}
                        className={cn(INPUT, "font-mono")}
                        value={r.key}
                        onChange={(e) =>
                          setRows(rows.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))
                        }
                      />
                    </td>
                    <td className="px-3 py-2">
                      <input
                        aria-label={t("Limit {n}", { n: i + 1 })}
                        type="number"
                        className={cn(INPUT, "w-24")}
                        value={r.limit}
                        onChange={(e) =>
                          setRows(
                            rows.map((x, j) => (j === i ? { ...x, limit: e.target.value } : x)),
                          )
                        }
                      />
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">
                      {tenant.usage?.[r.key] ?? 0}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={t("Remove quota {n}", { n: i + 1 })}
                        onClick={() => setRows(rows.filter((_, j) => j !== i))}
                      >
                        ×
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <Button
            size="sm"
            variant="outline"
            onClick={() => setRows([...rows, { key: "", limit: "10" }])}
          >
            {t("Add quota")}
          </Button>
        </section>
        <section className="flex items-start gap-3 rounded-lg border p-3">
          <div className="flex-1">
            <div className="font-medium">{t("Require approval")}</div>
            <p className="text-xs text-muted-foreground">
              {t(
                "Applies need a second tenant-admin. Tenant-admins can turn this on; only a system-admin can turn it off.",
              )}
            </p>
          </div>
          <Switch
            aria-label={t("Require approval")}
            checked={approval}
            onCheckedChange={setApproval}
          />
        </section>
        {(save.error || del.error) && <ErrorState error={save.error ?? del.error} />}
        <div className="flex items-center justify-between">
          <Button variant="ghost" onClick={() => setConfirm(true)}>
            {t("Delete tenant")}
          </Button>
          <Button disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
            {t("Save")}
          </Button>
        </div>
      </div>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={t("Delete tenant {name}?", { name: tenant.name })}
        confirmLabel={t("Delete tenant")}
        busy={del.isPending}
        onConfirm={async () => {
          try {
            await del.mutateAsync();
          } finally {
            setConfirm(false);
          }
        }}
      >
        <p>
          {t(
            "This removes the tenant and everything in it. The server refuses while the tenant still has objects or engines.",
          )}
        </p>
      </ConfirmDialog>
    </aside>
  );
}

function CreateTenant({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [approval, setApproval] = useState(false);
  const create = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants", {
          body: { name, quotas: {}, require_approval: approval },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "tenants"] });
      void qc.invalidateQueries({ queryKey: ["me"] });
      onClose();
    },
  });
  const nameError =
    create.error instanceof ApiError && create.error.status === 422
      ? create.error.problem?.errors?.[0]?.message
      : undefined;
  return (
    <Sheet
      open
      centered
      onOpenChange={(o) => !o && onClose()}
      title={t("Create tenant")}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button disabled={!name || create.isPending} onClick={() => create.mutate()}>
            {t("Create tenant")}
          </Button>
        </>
      }
    >
      {create.error && !nameError && <ErrorState error={create.error} />}
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Name")}
        <input
          aria-label={t("Name")}
          className={INPUT}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        {nameError && (
          <span role="alert" className="block text-xs font-normal text-status-down">
            {nameError}
          </span>
        )}
      </label>
      <label className="flex items-center gap-2 text-sm">
        <Switch
          aria-label={t("Require approval")}
          checked={approval}
          onCheckedChange={setApproval}
        />
        {t("Require approval")}
      </label>
    </Sheet>
  );
}

export function Tenants() {
  const list = useTenants();
  const [picked, setPicked] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const items = list.data ?? [];
  const sel = items.find((x) => x.id === picked) ?? items[0];
  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("Tenants")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("Changes here are immediate and recorded in the audit log")}
          </p>
        </div>
        <Button onClick={() => setCreating(true)}>{t("Create tenant")}</Button>
      </div>
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_26rem]">
        <div className="overflow-hidden rounded-lg border bg-card">
          {list.isPending ? (
            <TableSkeleton cols={4} />
          ) : list.isError ? (
            <ErrorState error={list.error} onRetry={() => void list.refetch()} />
          ) : items.length === 0 ? (
            <EmptyState title={t("No tenants yet")} body={t("Create the first tenant.")} />
          ) : (
            <table className="w-full text-[13px]">
              <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
                <tr>
                  {["Name", "Approval", "Members", "Quotas"].map((h) => (
                    <th key={h} className="px-4 py-2.5 font-medium">
                      {t(h)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {items.map((x) => (
                  <tr key={x.id} className={cn(sel?.id === x.id && "bg-accent/40")}>
                    <td className="px-4 py-3">
                      <button
                        type="button"
                        className="font-medium hover:underline"
                        aria-current={sel?.id === x.id}
                        onClick={() => setPicked(x.id)}
                      >
                        {x.name}
                      </button>
                    </td>
                    <td className="px-4 py-3">
                      <Badge tone={x.require_approval ? "pending" : "neutral"}>
                        {x.require_approval ? t("Required") : t("Off")}
                      </Badge>
                    </td>
                    <td className="px-4 py-3">{x.members ?? 0}</td>
                    <td className="px-4 py-3">{quotaLine(x.quotas)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        {sel && (
          <Detail key={`${sel.id}-${sel.version}`} tenant={sel} onDeleted={() => setPicked(null)} />
        )}
      </div>
      {creating && <CreateTenant onClose={() => setCreating(false)} />}
    </div>
  );
}
