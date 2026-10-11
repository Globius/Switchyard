/** Admin: everyone who has signed in, what they can do now, and disable, end sessions or delete. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useProviders } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { sinceText } from "@/features/access/Access";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type User = components["schemas"]["UserRead"];

const SELECT =
  "h-9 rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

/** "system-admin", "2 tenants", "1 tenant" or "no grants" for the Access column. */
export function accessText(u: Pick<User, "grants">): string {
  if (u.grants.some((g) => g.role === "system-admin")) return "system-admin";
  const n = new Set(u.grants.map((g) => g.tenant)).size;
  return n === 0 ? t("no grants") : n === 1 ? t("1 tenant") : t("{n} tenants", { n });
}

function Detail({ user }: { user: User }) {
  const qc = useQueryClient();
  const [confirm, setConfirm] = useState<"delete" | "sessions" | "disable" | null>(null);
  const run = useMutation({
    mutationFn: async (what: "delete" | "sessions" | "disable" | "enable") => {
      const path = { user: user.id };
      if (what === "delete")
        return unwrap(await api.DELETE("/api/v1/users/{user}", { params: { path } }));
      if (what === "sessions")
        return unwrap(await api.POST("/api/v1/users/{user}/revoke-sessions", { params: { path } }));
      if (what === "disable")
        return unwrap(await api.POST("/api/v1/users/{user}/disable", { params: { path } }));
      return unwrap(await api.POST("/api/v1/users/{user}/enable", { params: { path } }));
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["admin", "users"] }),
  });
  const disabled = user.disabled_at != null;
  const name = user.username;
  const row = (label: string, value: React.ReactNode) => (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd>{value}</dd>
    </>
  );
  return (
    <aside className="rounded-lg border bg-card" aria-label={t("User details")}>
      <header className="flex items-center gap-2 border-b px-4 py-3">
        <h2 className="text-base font-semibold">{name}</h2>
        {disabled ? (
          <StatusPill status="down">{t("Disabled")}</StatusPill>
        ) : (
          <StatusPill status="healthy">{t("Active")}</StatusPill>
        )}
      </header>
      <div className="space-y-4 p-4 text-sm">
        <dl className="grid grid-cols-[7rem_1fr] gap-y-2">
          {row(t("Username"), <span className="font-mono">{user.username}</span>)}
          {row(t("Email"), user.email ?? "—")}
          {row(t("Provider"), user.provider)}
          {row(t("First sign-in"), new Date(user.first_login_at).toLocaleDateString())}
          {row(
            t("Last sign-in"),
            `${sinceText(user.last_login_at)} · ${t("seen {when}", { when: sinceText(user.last_seen_at) })}`,
          )}
        </dl>
        <section className="space-y-1.5">
          <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("Groups at last sign-in")}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {user.groups.length === 0 && <span className="text-muted-foreground">—</span>}
            {user.groups.map((g) => (
              <Badge key={g} className="font-mono">
                {g}
              </Badge>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">
            {t(
              "Group changes at the provider show up at the next sign-in, at most four hours away.",
            )}
          </p>
        </section>
        <section className="space-y-1.5">
          <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            {t("What {name} can do now", { name })}
          </div>
          {user.grants.length === 0 ? (
            <p className="text-muted-foreground">
              {t("No grants: no group matches a role mapping.")}
            </p>
          ) : (
            <table className="w-full overflow-hidden rounded-md border text-[13px]">
              <thead className="text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-3 py-2 font-medium">{t("Tenant")}</th>
                  <th className="px-3 py-2 font-medium">{t("Role")}</th>
                  <th className="px-3 py-2 font-medium">{t("Granted by")}</th>
                </tr>
              </thead>
              <tbody className="divide-y">
                {user.grants.map((g, i) => (
                  <tr key={i}>
                    <td className="px-3 py-2">{g.tenant ?? t("All tenants")}</td>
                    <td className="px-3 py-2">
                      <Badge>{g.role}</Badge>
                    </td>
                    <td className="px-3 py-2 font-mono text-xs">
                      {g.via.map((v) => v.group).join(", ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
        {run.error && <ErrorState error={run.error} />}
        <div className="flex items-center gap-2">
          <Button variant="outline" disabled={run.isPending} onClick={() => setConfirm("sessions")}>
            {t("End sessions now")}
          </Button>
          <Button
            variant="outline"
            disabled={run.isPending}
            onClick={() => (disabled ? run.mutate("enable") : setConfirm("disable"))}
          >
            {disabled ? t("Enable") : t("Disable")}
          </Button>
          <Button variant="ghost" className="ml-auto" onClick={() => setConfirm("delete")}>
            {t("Delete")}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          {t(
            "Disabling refuses sign-in and ends sessions. Deleting only removes this record; {name} reappears at the next sign-in unless disabled or unmapped.",
            { name },
          )}
        </p>
      </div>
      <ConfirmDialog
        open={confirm !== null}
        onOpenChange={(o) => !o && setConfirm(null)}
        title={
          confirm === "delete"
            ? t("Delete {name}?", { name })
            : confirm === "disable"
              ? t("Disable {name}?", { name })
              : t("End sessions for {name}?", { name })
        }
        confirmLabel={
          confirm === "delete"
            ? t("Delete")
            : confirm === "disable"
              ? t("Disable")
              : t("End sessions")
        }
        busy={run.isPending}
        onConfirm={async () => {
          if (!confirm) return;
          try {
            await run.mutateAsync(confirm);
          } finally {
            setConfirm(null);
          }
        }}
      >
        <p>{t("This acts immediately and is recorded in the audit log.")}</p>
      </ConfirmDialog>
    </aside>
  );
}

export function Users() {
  const providers = useProviders();
  const tenants = useQuery({
    queryKey: ["admin", "tenants"],
    queryFn: async () => unwrap(await api.GET("/api/v1/tenants")),
  });
  const [q, setQ] = useState("");
  const [provider, setProvider] = useState("");
  const [tenant, setTenant] = useState("");
  const [disabledOnly, setDisabledOnly] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);
  const list = useQuery({
    queryKey: ["admin", "users", { q, provider, tenant, disabledOnly }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/users", {
          params: {
            query: {
              q: q || undefined,
              provider: provider || undefined,
              tenant: tenant || undefined,
              disabled: disabledOnly || undefined,
              limit: 200,
            },
          },
        }),
      ),
  });
  const items = list.data?.items ?? [];
  const sel = items.find((u) => u.id === picked) ?? items[0];
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-2xl font-semibold">{t("Users")}</h1>
        <p className="text-sm text-muted-foreground">
          {t(
            "Everyone who has signed in through an identity provider. Access itself comes from role mappings.",
          )}
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <input
          aria-label={t("Search users")}
          placeholder={t("Name, username or email")}
          className={cn(SELECT, "w-64")}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select
          aria-label={t("Provider")}
          className={SELECT}
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
        >
          <option value="">{t("All providers")}</option>
          {providers.data?.providers.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
        <select
          aria-label={t("Tenant")}
          className={SELECT}
          value={tenant}
          onChange={(e) => setTenant(e.target.value)}
        >
          <option value="">{t("All tenants")}</option>
          {tenants.data?.map((x) => (
            <option key={x.id} value={x.name}>
              {x.name}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={disabledOnly}
            onChange={(e) => setDisabledOnly(e.target.checked)}
          />
          {t("Disabled only")}
        </label>
      </div>
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_28rem]">
        <div className="overflow-hidden rounded-lg border bg-card">
          {list.isPending ? (
            <TableSkeleton cols={5} />
          ) : list.isError ? (
            <ErrorState error={list.error} onRetry={() => void list.refetch()} />
          ) : items.length === 0 ? (
            <EmptyState title={t("No users")} body={t("Nobody matches those filters.")} />
          ) : (
            <table className="w-full text-[13px]">
              <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
                <tr>
                  {["Person", "Provider", "Access", "Last seen", "Status"].map((h) => (
                    <th key={h} className="px-4 py-2.5 font-medium">
                      {t(h)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {items.map((u) => (
                  <tr key={u.id} className={cn(sel?.id === u.id && "bg-accent/40")}>
                    <td className="px-4 py-3">
                      <button
                        type="button"
                        className="text-left font-medium hover:underline"
                        aria-current={sel?.id === u.id}
                        onClick={() => setPicked(u.id)}
                      >
                        {u.username}
                      </button>
                      <div className="text-xs text-muted-foreground">{u.email ?? u.username}</div>
                    </td>
                    <td className="px-4 py-3">{u.provider}</td>
                    <td className="px-4 py-3">{accessText(u)}</td>
                    <td className="px-4 py-3">{sinceText(u.last_seen_at)}</td>
                    <td className="px-4 py-3">
                      {u.disabled_at ? (
                        <StatusPill status="down">{t("Disabled")}</StatusPill>
                      ) : u.grants.length === 0 ? (
                        <StatusPill status="neutral">{t("No access")}</StatusPill>
                      ) : (
                        <StatusPill status="healthy">{t("Active")}</StatusPill>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        {sel && <Detail key={`${sel.id}-${sel.disabled_at}`} user={sel} />}
      </div>
    </div>
  );
}
