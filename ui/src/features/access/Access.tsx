// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Tenant access and settings: members, role mappings, API tokens, and the approval setting. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan, useMe, useProviders } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ShowOnceSecret } from "@/components/sy/ShowOnceSecret";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Role = components["schemas"]["Role"];
type Mapping = components["schemas"]["RoleMappingRead"];
const ROLES: Role[] = ["viewer", "operator", "tenant-admin"];
const RANK: Record<Role, number> = { viewer: 0, operator: 1, "tenant-admin": 2, "system-admin": 3 };

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

type TabKey = "members" | "mappings" | "tokens" | "settings";
const TABS: { key: TabKey; label: string }[] = [
  { key: "members", label: "Members" },
  { key: "mappings", label: "Role mappings" },
  { key: "tokens", label: "API tokens" },
  { key: "settings", label: "Settings" },
];

function roleOf(me: ReturnType<typeof useMe>["data"], tenant: string): Role | undefined {
  return me?.tenants.find((x) => x.name === tenant || x.id === tenant)?.role;
}

/** Relative day for a sign-in time: today, yesterday, or the short date. */
export function sinceText(iso: string, now = new Date()): string {
  const d = new Date(iso);
  const days = Math.floor(
    (now.setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 86_400_000,
  );
  if (days <= 0)
    return `${t("today")} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })}`;
  if (days === 1) return t("yesterday");
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function Members({ tenant }: { tenant: string }) {
  const [q, setQ] = useState("");
  const list = useQuery({
    queryKey: ["t", tenant, "users", { q }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/users", {
          params: { path: { t: tenant }, query: { q: q || undefined, limit: 200 } },
        }),
      ),
  });
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <input
          aria-label={t("Search people")}
          placeholder={t("Search name or username")}
          className={cn(INPUT, "max-w-72")}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <span className="text-xs text-muted-foreground">
          {t("People with a grant in {tenant}, from their last sign-in", { tenant })}
        </span>
      </div>
      <div className="overflow-hidden rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={4} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : list.data.items.length === 0 ? (
          <EmptyState title={t("No one yet")} body={t("People appear here after they sign in.")} />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["Person", "Role here", "Granted by", "Last sign-in"].map((h) => (
                  <th key={h} className="px-4 py-2.5 font-medium">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {list.data.items.map((u) => {
                const g = u.grants[0];
                return (
                  <tr key={u.id}>
                    <td className="px-4 py-3">
                      <div className="font-medium">{u.username}</div>
                      <div className="text-xs text-muted-foreground">
                        {u.email ?? u.username} · {u.provider}
                      </div>
                    </td>
                    <td className="px-4 py-3">{g && <Badge>{g.role}</Badge>}</td>
                    <td className="px-4 py-3 font-mono text-xs">
                      {g?.via?.length
                        ? g.via.map((v) => `${v.provider} · ${v.group}`).join(", ")
                        : "—"}
                    </td>
                    <td className="px-4 py-3">{sinceText(u.last_login_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {t(
          "Several matching groups give the highest role. A system-admin can disable people or end their sessions from Admin › Users.",
        )}
      </p>
    </div>
  );
}

function MappingSheet({ tenant, onClose }: { tenant: string; onClose: () => void }) {
  const providers = useProviders();
  const qc = useQueryClient();
  const [provider, setProvider] = useState("");
  const [group, setGroup] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const add = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/role-mappings", {
          params: { path: { t: tenant } },
          body: { provider, group_name: group, role, label: null },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["t", tenant, "role-mappings"] });
      onClose();
    },
  });
  return (
    <Sheet
      open
      centered
      onOpenChange={(o) => !o && onClose()}
      title={t("Add role mapping")}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button disabled={!provider || !group || add.isPending} onClick={() => add.mutate()}>
            {t("Add mapping")}
          </Button>
        </>
      }
    >
      {add.error && <ErrorState error={add.error} />}
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Provider")}
        <select
          aria-label={t("Provider")}
          className={INPUT}
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
        >
          <option value="">{t("Choose…")}</option>
          {providers.data?.providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </label>
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Group")}
        <input
          aria-label={t("Group")}
          className={cn(INPUT, "font-mono")}
          value={group}
          onChange={(e) => setGroup(e.target.value)}
        />
      </label>
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Role")}
        <select
          aria-label={t("Role")}
          className={INPUT}
          value={role}
          onChange={(e) => setRole(e.target.value as Role)}
        >
          <option value="viewer">viewer</option>
          <option value="operator">operator</option>
        </select>
      </label>
    </Sheet>
  );
}

function Mappings({ tenant }: { tenant: string }) {
  const can = useCan(tenant);
  const manage = can("manage", "role_mapping");
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<Mapping | null>(null);
  const list = useQuery({
    queryKey: ["t", tenant, "role-mappings"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/role-mappings", { params: { path: { t: tenant } } }),
      ),
  });
  const del = useMutation({
    mutationFn: async (m: Mapping) =>
      unwrap(
        await api.DELETE("/api/v1/tenants/{t}/role-mappings/{mapping}", {
          params: { path: { t: tenant, mapping: m.id } },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["t", tenant, "role-mappings"] }),
  });
  const rows = (list.data ?? []) as Mapping[];
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {t("Groups from your identity providers decide who gets which role in {tenant}.", {
            tenant,
          })}
        </p>
        <Tip label={manage.reason}>
          <Button disabled={!manage.allowed} onClick={() => setAdding(true)}>
            {t("Add mapping")}
          </Button>
        </Tip>
      </div>
      <div className="overflow-hidden rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={4} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState
            title={t("No role mappings")}
            body={t("Without a mapping nobody can sign in to this tenant.")}
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["Provider", "Group", "Role", ""].map((h, i) => (
                  <th key={i} className="px-4 py-2.5 font-medium">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((m) => {
                const locked = RANK[m.role] >= RANK["tenant-admin"];
                return (
                  <tr key={m.id}>
                    <td className="px-4 py-3">{m.provider}</td>
                    <td className="px-4 py-3 font-mono text-xs">{m.group_name}</td>
                    <td className="px-4 py-3">
                      <Badge>{m.role}</Badge>
                    </td>
                    <td className="px-4 py-3 text-right">
                      {locked ? (
                        <span className="text-xs text-muted-foreground">{t("locked")}</span>
                      ) : (
                        <Tip label={manage.reason}>
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={!manage.allowed}
                            onClick={() => setRemoving(m)}
                          >
                            {t("Remove")}
                          </Button>
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
      <p className="text-xs text-muted-foreground">
        {t(
          "You can map groups to viewer or operator here. Group names match exactly, case-sensitive. Changes reach people already signed in within a minute.",
        )}
      </p>
      {adding && <MappingSheet tenant={tenant} onClose={() => setAdding(false)} />}
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(o) => !o && setRemoving(null)}
        title={t("Remove mapping for {group}?", { group: removing?.group_name ?? "" })}
        confirmLabel={t("Remove mapping")}
        busy={del.isPending}
        onConfirm={async () => {
          if (!removing) return;
          try {
            await del.mutateAsync(removing);
          } finally {
            setRemoving(null);
          }
        }}
      >
        <p>{t("People who only had this role through that group lose it within a minute.")}</p>
      </ConfirmDialog>
    </div>
  );
}

function Tokens({ tenant, myRole }: { tenant: string; myRole: Role | undefined }) {
  const can = useCan(tenant);
  const manage = can("manage", "token");
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [ttl, setTtl] = useState("90");
  const [shown, setShown] = useState<string | null>(null);
  const [stored, setStored] = useState(false);
  const [revoking, setRevoking] = useState<{ id: string; name: string } | null>(null);
  const list = useQuery({
    queryKey: ["t", tenant, "tokens"],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/tokens", { params: { path: { t: tenant } } })),
  });
  const create = useMutation({
    // The created token is shown once; do not keep it in the mutation cache.
    gcTime: 0,
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/tokens", {
          params: { path: { t: tenant } },
          body: { name, role, ttl_days: ttl ? Number(ttl) : null },
        }),
      ),
    onSuccess: (r) => {
      setShown(r.token);
      setName("");
      void qc.invalidateQueries({ queryKey: ["t", tenant, "tokens"] });
    },
  });
  const revoke = useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await api.DELETE("/api/v1/tenants/{t}/tokens/{token}", {
          params: { path: { t: tenant, token: id } },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["t", tenant, "tokens"] }),
  });
  const allowed = ROLES.filter((r) => RANK[r] <= RANK[myRole ?? "viewer"]);
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-4">
        <p className="text-sm text-muted-foreground">
          {t(
            "Tokens for syctl, CI and integrations in {tenant}. Shown once at creation; a token can't create tokens.",
            { tenant },
          )}
        </p>
        <Tip label={manage.reason}>
          <Button disabled={!manage.allowed} onClick={() => setCreating(true)}>
            {t("Create token")}
          </Button>
        </Tip>
      </div>
      <div className="overflow-hidden rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={5} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : list.data.length === 0 ? (
          <EmptyState title={t("No API tokens")} body={t("Create one for syctl or a CI job.")} />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["Name", "Role", "Expires", "Status", ""].map((h, i) => (
                  <th key={i} className="px-4 py-2.5 font-medium">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {list.data.map((k) => (
                <tr key={k.id}>
                  <td className="px-4 py-3 font-mono text-xs">{k.name}</td>
                  <td className="px-4 py-3">
                    <Badge>{k.role}</Badge>
                  </td>
                  <td className="px-4 py-3">
                    {k.expires_at ? new Date(k.expires_at).toLocaleDateString() : t("Never")}
                  </td>
                  <td className="px-4 py-3">
                    {k.revoked ? (
                      <StatusPill status="neutral">{t("Revoked")}</StatusPill>
                    ) : (
                      <StatusPill status="healthy">{t("Active")}</StatusPill>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    {!k.revoked && (
                      <Tip label={manage.reason}>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={!manage.allowed}
                          onClick={() => setRevoking({ id: k.id, name: k.name })}
                        >
                          {t("Revoke")}
                        </Button>
                      </Tip>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {t("Roles go up to your own ({role}). Default lifetime 90 days.", {
          role: myRole ?? "viewer",
        })}
      </p>
      {creating && (
        <Sheet
          open
          centered
          onOpenChange={(o) => {
            if (!o && !(shown && !stored)) {
              setCreating(false);
              setShown(null);
              create.reset();
              setStored(false);
            }
          }}
          title={t("Create token")}
          footer={
            <>
              <Button
                variant="ghost"
                className="ml-auto"
                disabled={!!shown && !stored}
                onClick={() => {
                  setCreating(false);
                  setShown(null);
                  create.reset();
                  setStored(false);
                }}
              >
                {shown ? t("Done") : t("Cancel")}
              </Button>
              {!shown && (
                <Button disabled={!name || create.isPending} onClick={() => create.mutate()}>
                  {t("Create token")}
                </Button>
              )}
            </>
          }
        >
          {create.error && <ErrorState error={create.error} />}
          {shown ? (
            <ShowOnceSecret
              secrets={[{ label: t("Token"), value: shown }]}
              stored={stored}
              onStoredChange={setStored}
            />
          ) : (
            <>
              <label className="block space-y-1.5 text-sm font-medium">
                {t("Name")}
                <input
                  aria-label={t("Token name")}
                  className={INPUT}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
              <div className="grid grid-cols-2 gap-3">
                <label className="block space-y-1.5 text-sm font-medium">
                  {t("Role")}
                  <select
                    aria-label={t("Token role")}
                    className={INPUT}
                    value={role}
                    onChange={(e) => setRole(e.target.value as Role)}
                  >
                    {allowed.map((r) => (
                      <option key={r} value={r}>
                        {r}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block space-y-1.5 text-sm font-medium">
                  {t("Lifetime (days)")}
                  <input
                    aria-label={t("Lifetime (days)")}
                    type="number"
                    className={INPUT}
                    value={ttl}
                    onChange={(e) => setTtl(e.target.value)}
                  />
                </label>
              </div>
            </>
          )}
        </Sheet>
      )}
      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(o) => !o && setRevoking(null)}
        title={t("Revoke {name}?", { name: revoking?.name ?? "" })}
        confirmLabel={t("Revoke token")}
        busy={revoke.isPending}
        onConfirm={async () => {
          if (!revoking) return;
          try {
            await revoke.mutateAsync(revoking.id);
          } finally {
            setRevoking(null);
          }
        }}
      >
        <p>{t("Anything using this token stops working immediately.")}</p>
      </ConfirmDialog>
    </div>
  );
}

function TenantSettings({ tenant }: { tenant: string }) {
  const can = useCan(tenant);
  const enable = can("enable", "require_approval");
  const disable = can("disable", "require_approval");
  const qc = useQueryClient();
  const info = useQuery({
    queryKey: ["t", tenant, "tenant"],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}", { params: { path: { t: tenant } } })),
  });
  const patch = useMutation({
    mutationFn: async (v: boolean) =>
      unwrap(
        await api.PATCH("/api/v1/tenants/{t}", {
          params: { path: { t: tenant }, header: { "if-match": String(info.data!.version) } },
          body: { require_approval: v },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["t", tenant] }),
  });
  if (info.isPending) return <TableSkeleton rows={2} cols={2} />;
  if (info.isError) return <ErrorState error={info.error} onRetry={() => void info.refetch()} />;
  const on = info.data.require_approval;
  const gate = on ? disable : enable;
  const quotas = Object.entries(info.data.quotas ?? {});
  return (
    <div className="max-w-3xl space-y-5">
      <section className="space-y-2 rounded-lg border bg-card p-4">
        <div className="flex items-center gap-3">
          <h2 className="text-base font-semibold">{t("Require approval")}</h2>
          <Tip label={gate.reason}>
            <span>
              <Switch
                aria-label={t("Require approval")}
                checked={on}
                disabled={!gate.allowed || patch.isPending}
                onCheckedChange={(v) => patch.mutate(v)}
              />
            </span>
          </Tip>
        </div>
        <p className="text-sm text-muted-foreground">
          {t("Every apply needs a second tenant-admin.")}
        </p>
        <p className="text-xs text-muted-foreground">
          {t("Tenant-admins can turn it on; only a system-admin can turn it off.")}
        </p>
        {patch.error && <ErrorState error={patch.error} />}
      </section>
      <section className="space-y-2 rounded-lg border bg-card p-4">
        <h2 className="text-base font-semibold">{t("Quotas")}</h2>
        {quotas.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("No limits set.")}</p>
        ) : (
          <dl className="grid grid-cols-[14rem_1fr] gap-y-1 text-sm">
            {quotas.map(([k, v]) => {
              const used = info.data.usage?.[k] ?? 0;
              return (
                <div key={k} className="contents">
                  <dt className="text-muted-foreground">{k.replace(/_/g, " ")}</dt>
                  <dd className={used >= v ? "font-medium text-status-down" : undefined}>
                    {t("{used} of {limit}", { used, limit: v })}
                  </dd>
                </div>
              );
            })}
          </dl>
        )}
        <p className="text-xs text-muted-foreground">{t("Set by a system-admin.")}</p>
      </section>
    </div>
  );
}

export function Access({ tenant }: { tenant: string }) {
  const { data: me } = useMe();
  const [tab, setTab] = useState<TabKey>("members");
  const role = roleOf(me, tenant);
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">{t("Access and settings")}</h1>
        <p className="text-sm text-muted-foreground">
          {role
            ? t("Who can use {tenant}, and how. You are a {role} here.", { tenant, role })
            : t("Who can use {tenant}, and how.", { tenant })}
        </p>
      </div>
      <div role="tablist" className="flex gap-1 overflow-x-auto border-b">
        {TABS.map((x) => (
          <button
            key={x.key}
            role="tab"
            type="button"
            aria-selected={tab === x.key}
            onClick={() => setTab(x.key)}
            className={cn(
              "-mb-px border-b-2 border-transparent px-4 py-2 text-sm text-muted-foreground",
              tab === x.key && "border-primary font-medium text-foreground",
            )}
          >
            {t(x.label)}
          </button>
        ))}
      </div>
      {tab === "members" && <Members tenant={tenant} />}
      {tab === "mappings" && <Mappings tenant={tenant} />}
      {tab === "tokens" && <Tokens tenant={tenant} myRole={role} />}
      {tab === "settings" && <TenantSettings tenant={tenant} />}
    </div>
  );
}
