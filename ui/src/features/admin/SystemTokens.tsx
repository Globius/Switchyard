/** Admin: system API tokens (system-admin role), shown once at creation. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ShowOnceSecret } from "@/components/sy/ShowOnceSecret";
import { Sheet } from "@/components/ui/sheet";
import { t } from "@/lib/i18n";

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function SystemTokens() {
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [ttl, setTtl] = useState("90");
  const [shown, setShown] = useState<string | null>(null);
  const [stored, setStored] = useState(false);
  const [revoking, setRevoking] = useState<{ id: string; name: string } | null>(null);
  const list = useQuery({
    queryKey: ["admin", "tokens"],
    queryFn: async () => unwrap(await api.GET("/api/v1/system/tokens")),
  });
  const create = useMutation({
    // The created token is shown once; do not keep it in the mutation cache.
    gcTime: 0,
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/system/tokens", {
          body: { name, ttl_days: Number(ttl) || 90 },
        }),
      ),
    onSuccess: (r) => {
      setShown(r.token);
      setName("");
      void qc.invalidateQueries({ queryKey: ["admin", "tokens"] });
    },
  });
  const revoke = useMutation({
    mutationFn: async (id: string) =>
      unwrap(
        await api.DELETE("/api/v1/system/tokens/{token}", { params: { path: { token: id } } }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["admin", "tokens"] }),
  });
  const close = () => {
    setCreating(false);
    setShown(null);
    create.reset();
    setStored(false);
  };
  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("System tokens")}</h1>
          <p className="text-sm text-muted-foreground">
            {t(
              "Tokens with the system-admin role, for automation that spans tenants. Shown once at creation.",
            )}
          </p>
        </div>
        <Button onClick={() => setCreating(true)}>{t("Create token")}</Button>
      </div>
      <div className="overflow-hidden rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={6} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : list.data.length === 0 ? (
          <EmptyState
            title={t("No system tokens")}
            body={t("Create one for cross-tenant automation.")}
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["Name", "Role", "Created by", "Last used", "Expires", "Status", ""].map(
                  (h, i) => (
                    <th key={i} className="px-4 py-2.5 font-medium">
                      {t(h)}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody className="divide-y">
              {list.data.map((k) => (
                <tr key={k.id}>
                  <td className="px-4 py-3 font-mono text-xs">{k.name}</td>
                  <td className="px-4 py-3">
                    <Badge>{k.role}</Badge>
                  </td>
                  <td className="px-4 py-3">{k.created_by}</td>
                  <td className="px-4 py-3">
                    {k.last_used_at ? new Date(k.last_used_at).toLocaleDateString() : t("never")}
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
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setRevoking({ id: k.id, name: k.name })}
                      >
                        {t("Revoke")}
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {creating && (
        <Sheet
          open
          centered
          onOpenChange={(o) => !o && !(shown && !stored) && close()}
          title={t("Create system token")}
          footer={
            <>
              <Button
                variant="ghost"
                className="ml-auto"
                disabled={!!shown && !stored}
                onClick={close}
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
