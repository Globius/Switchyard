// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Admin: system-wide and per-tenant role mappings, plus what each role can do (from the server's permission list). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check } from "lucide-react";
import { Fragment, useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useProviders, usePermissions } from "@/auth/session";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Mapping = components["schemas"]["RoleMappingRead"];
type Role = components["schemas"]["Role"];
const ROLES: Role[] = ["viewer", "operator", "tenant-admin", "system-admin"];
const TENANT_ROLES: Role[] = ["viewer", "operator", "tenant-admin"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

function useMappings() {
  return useQuery({
    queryKey: ["admin", "role-mappings"],
    queryFn: async () => unwrap(await api.GET("/api/v1/role-mappings")),
  });
}

function useTenantList() {
  return useQuery({
    queryKey: ["admin", "tenants"],
    queryFn: async () => unwrap(await api.GET("/api/v1/tenants")),
  });
}

/** `Miguel Rodriguez · Oct 2` */
function added(m: Mapping): string {
  return `${m.created_by} · ${new Date(m.created_at).toLocaleDateString([], { month: "short", day: "numeric" })}`;
}

function Row({
  m,
  roles,
  onRole,
  onRemove,
  busy,
}: {
  m: Mapping;
  roles: Role[];
  onRole: (r: Role) => void;
  onRemove: () => void;
  busy: boolean;
}) {
  return (
    <tr>
      <td className="px-4 py-3">{m.provider}</td>
      <td className="px-4 py-3 font-mono text-xs">{m.group_name}</td>
      <td className="px-4 py-3">{m.label ?? "—"}</td>
      <td className="px-4 py-3">
        <select
          aria-label={t("Role of {group}", { group: m.group_name })}
          className={cn(INPUT, "w-40")}
          disabled={busy}
          value={m.role}
          onChange={(e) => onRole(e.target.value as Role)}
        >
          {roles.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
      </td>
      <td className="px-4 py-3 text-muted-foreground">{added(m)}</td>
      <td className="px-4 py-3 text-right">
        <Button
          size="sm"
          variant="ghost"
          aria-label={t("Remove mapping for {group}", { group: m.group_name })}
          disabled={busy}
          onClick={onRemove}
        >
          ×
        </Button>
      </td>
    </tr>
  );
}

function AddRow({ tenantId, roles }: { tenantId: string | null; roles: Role[] }) {
  const providers = useProviders();
  const qc = useQueryClient();
  const [provider, setProvider] = useState("");
  const [group, setGroup] = useState("");
  const [label, setLabel] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const add = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/role-mappings", {
          body: {
            provider,
            group_name: group,
            role,
            label: label || null,
            tenant_id: tenantId,
          },
        }),
      ),
    onSuccess: () => {
      setGroup("");
      setLabel("");
      void qc.invalidateQueries({ queryKey: ["admin", "role-mappings"] });
    },
  });
  return (
    <>
      <div className="flex flex-wrap items-center gap-3 border-t bg-muted/40 px-4 py-3">
        <select
          aria-label={t("Provider")}
          className={cn(INPUT, "w-40")}
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
        >
          <option value="">{t("Provider")}</option>
          {providers.data?.providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <input
          aria-label={t("Group name or object ID")}
          placeholder={t("Group name or object ID")}
          className={cn(INPUT, "w-56 font-mono")}
          value={group}
          onChange={(e) => setGroup(e.target.value)}
        />
        <input
          aria-label={t("Label for opaque IDs")}
          placeholder={t("Label for opaque IDs")}
          className={cn(INPUT, "w-48")}
          value={label}
          onChange={(e) => setLabel(e.target.value)}
        />
        <select
          aria-label={t("Role")}
          className={cn(INPUT, "w-40")}
          value={role}
          onChange={(e) => setRole(e.target.value as Role)}
        >
          {roles.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <Button disabled={!provider || !group || add.isPending} onClick={() => add.mutate()}>
          {t("Add")}
        </Button>
      </div>
      {add.error && <ErrorState error={add.error} />}
    </>
  );
}

function Table({
  rows,
  roles,
  tenantId,
  withAdd,
}: {
  rows: Mapping[];
  roles: Role[];
  tenantId: string | null;
  withAdd: boolean;
}) {
  const qc = useQueryClient();
  const patch = useMutation({
    mutationFn: async ({ m, role }: { m: Mapping; role: Role }) =>
      unwrap(
        await api.PATCH("/api/v1/role-mappings/{mapping}", {
          params: { path: { mapping: m.id }, header: { "if-match": String(m.version) } },
          body: { role },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["admin", "role-mappings"] }),
  });
  const del = useMutation({
    mutationFn: async (m: Mapping) =>
      unwrap(
        await api.DELETE("/api/v1/role-mappings/{mapping}", {
          params: { path: { mapping: m.id } },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["admin", "role-mappings"] }),
  });
  const busy = patch.isPending || del.isPending;
  return (
    <div>
      {(patch.error || del.error) && <ErrorState error={patch.error ?? del.error} />}
      {rows.length === 0 ? (
        <p className="px-4 py-3 text-sm text-muted-foreground">{t("No mappings yet.")}</p>
      ) : (
        <table className="w-full text-[13px]">
          <thead className="text-left text-xs text-muted-foreground">
            <tr>
              {["Provider", "Group", "Label", "Role", "Added", ""].map((h, i) => (
                <th key={i} className="px-4 py-2.5 font-medium">
                  {t(h)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y">
            {rows.map((m) => (
              <Row
                key={m.id}
                m={m}
                roles={roles}
                busy={busy}
                onRole={(role) => patch.mutate({ m, role })}
                onRemove={() => del.mutate(m)}
              />
            ))}
          </tbody>
        </table>
      )}
      {withAdd && <AddRow tenantId={tenantId} roles={roles} />}
    </div>
  );
}

const GROUPS = [
  "Read",
  "Applications",
  "Infrastructure",
  "Change sets",
  "Runtime",
  "Tenant settings",
  "Access",
  "System",
];

/** Permissions grouped as the server labels them; the group row shows the lowest role that has any of them. */
export function groupPermissions<T extends { group: string; min_role: Role }>(
  perms: T[],
): { group: string; min: Role; items: T[] }[] {
  const rank = (r: Role) => ROLES.indexOf(r);
  const names = [...GROUPS, ...new Set(perms.map((p) => p.group))].filter(
    (g, i, all) => all.indexOf(g) === i,
  );
  return names
    .map((group) => {
      const items = perms.filter((p) => p.group === group);
      const min = items.reduce<Role>(
        (m, p) => (rank(p.min_role) < rank(m) ? p.min_role : m),
        "system-admin",
      );
      return { group, min, items };
    })
    .filter((g) => g.items.length > 0);
}

function RoleMatrix() {
  const matrix = usePermissions();
  const [open, setOpen] = useState<string | null>(null);
  if (!matrix.data) return null;
  const rank = (r: Role) => ROLES.indexOf(r);
  const groups = groupPermissions(
    matrix.data.permissions.map((p) => ({ ...p, group: p.group ?? "Other" })),
  );
  const ticks = (min: Role) =>
    ROLES.map((r) => (
      <td key={r} className="px-3 py-2.5 text-center">
        {rank(r) >= rank(min) ? (
          <Check className="mx-auto size-4" aria-label={t("allowed")} />
        ) : (
          <span className="text-muted-foreground" aria-label={t("not allowed")}>
            –
          </span>
        )}
      </td>
    ));
  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <header className="flex items-baseline gap-3 border-b px-4 py-3">
        <h2 className="text-base font-semibold">{t("What each role can do")}</h2>
        <span className="text-xs text-muted-foreground">
          {t("roles nest: each can do everything to its left")}
        </span>
      </header>
      <table className="w-full text-[13px]">
        <thead className="text-left text-xs text-muted-foreground">
          <tr>
            <th className="px-4 py-2 font-medium">{t("Action")}</th>
            {ROLES.map((r) => (
              <th key={r} className="px-3 py-2 text-center font-medium">
                {r}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y">
          {groups.map((g) => (
            <Fragment key={g.group}>
              <tr>
                <td className="px-4 py-2.5">
                  <button
                    type="button"
                    aria-expanded={open === g.group}
                    className="text-left font-medium hover:underline"
                    onClick={() => setOpen(open === g.group ? null : g.group)}
                  >
                    {t(g.group)}
                  </button>{" "}
                  <span className="text-xs text-muted-foreground">
                    {t("{n} permissions", { n: g.items.length })}
                  </span>
                </td>
                {ticks(g.min)}
              </tr>
              {open === g.group &&
                g.items.map((p) => (
                  <tr key={`${p.verb}-${p.object}`} className="bg-muted/30">
                    <td className="px-4 py-2 pl-8 text-muted-foreground">{p.description}</td>
                    {ticks(p.min_role)}
                  </tr>
                ))}
            </Fragment>
          ))}
        </tbody>
      </table>
      <p className="border-t px-4 py-3 text-xs text-muted-foreground">
        {t(
          "Read from the server's permission list, so disabled buttons always match what the API enforces. Open a group to see each permission.",
        )}
      </p>
    </section>
  );
}

export function RoleMappings() {
  const list = useMappings();
  const tenants = useTenantList();
  const [picked, setPicked] = useState<string | null>(null);
  const rows = (list.data ?? []) as Mapping[];
  const system = rows.filter((m) => m.tenant_id == null);
  const tenantItems = tenants.data ?? [];
  const tenant = tenantItems.find((x) => x.id === picked) ?? tenantItems[0];
  const forTenant = (id: string) => rows.filter((m) => m.tenant_id === id);
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold">{t("Role mappings")}</h1>
        <p className="text-sm text-muted-foreground">
          {t(
            "Identity-provider groups grant roles. Several matching groups give the highest role. Edits reach people already signed in within a minute.",
          )}
        </p>
      </div>
      {list.isPending ? (
        <TableSkeleton cols={5} />
      ) : list.isError ? (
        <ErrorState error={list.error} onRetry={() => void list.refetch()} />
      ) : (
        <>
          <section className="overflow-hidden rounded-lg border bg-card">
            <header className="flex items-baseline gap-3 border-b px-4 py-3">
              <h2 className="text-base font-semibold">{t("System-wide")}</h2>
              <span className="text-xs text-muted-foreground">
                {t("system-admin in every tenant and the admin area")}
              </span>
            </header>
            <Table rows={system} roles={ROLES} tenantId={null} withAdd />
          </section>
          <section className="overflow-hidden rounded-lg border bg-card">
            <header className="border-b px-4 py-3">
              <h2 className="text-base font-semibold">{t("Per tenant")}</h2>
            </header>
            {tenantItems.length === 0 ? (
              <EmptyState title={t("No tenants yet")} body={t("Create a tenant first.")} />
            ) : (
              <>
                <div role="tablist" className="flex gap-1 overflow-x-auto border-b px-4">
                  {tenantItems.map((x) => (
                    <button
                      key={x.id}
                      role="tab"
                      type="button"
                      aria-selected={tenant?.id === x.id}
                      onClick={() => setPicked(x.id)}
                      className={cn(
                        "-mb-px flex items-center gap-2 border-b-2 border-transparent px-4 py-2 text-sm text-muted-foreground",
                        tenant?.id === x.id && "border-primary font-medium text-foreground",
                      )}
                    >
                      {x.name}
                      <Badge>{forTenant(x.id).length}</Badge>
                    </button>
                  ))}
                </div>
                {tenant && (
                  <Table
                    key={tenant.id}
                    rows={forTenant(tenant.id)}
                    roles={TENANT_ROLES}
                    tenantId={tenant.id}
                    withAdd
                  />
                )}
                <p className="border-t px-4 py-3 text-xs text-muted-foreground">
                  {t(
                    "Group names match exactly and are case-sensitive. Entra ID sends object IDs, so give them a label. Tenant-admins can add viewer and operator mappings for their own tenant themselves.",
                  )}
                </p>
              </>
            )}
          </section>
          <RoleMatrix />
        </>
      )}
    </div>
  );
}
