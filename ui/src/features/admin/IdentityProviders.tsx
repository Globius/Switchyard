/** Admin: identity providers (OIDC and SAML) and the break-glass administrator. Changes are immediate and audited. */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { ShowOnceSecret } from "@/components/sy/ShowOnceSecret";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Provider =
  components["schemas"]["OidcProviderRead"] | components["schemas"]["SamlProviderRead"];
type Oidc = components["schemas"]["OidcProviderRead"];
type Saml = components["schemas"]["SamlProviderRead"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

const isSaml = (p: Provider): p is Saml => p.protocol === "saml";

function useProviderList() {
  return useQuery({
    queryKey: ["admin", "identity-providers"],
    queryFn: async () => unwrap(await api.GET("/api/v1/identity-providers")),
  });
}

/** Mapping counts per provider, for "5 across 3 tenants". */
export function mappingSummary(
  rows: { provider_id: string; tenant_id: string | null; tenant: string | null }[],
  providerId: string,
): string {
  const mine = rows.filter((r) => r.provider_id === providerId);
  if (mine.length === 0) return t("none");
  const tenants = new Set(mine.map((r) => r.tenant_id ?? "system"));
  if (tenants.size === 1 && mine[0]!.tenant)
    return t("{n} in {tenant}", { n: mine.length, tenant: mine[0]!.tenant });
  return t("{n} across {m} tenants", { n: mine.length, m: tenants.size });
}

type Form = Record<string, string>;

const OIDC_FIELDS: [string, string, boolean?][] = [
  ["issuer_url", "Issuer URL", true],
  ["client_id", "Client ID", true],
  ["client_secret", "Client secret (write-only)", true],
  ["client_secret_ref", "Client secret reference", true],
  ["scopes", "Scopes"],
  ["username_claim", "Username claim"],
  ["email_claim", "Email claim"],
  ["groups_claim", "Groups claim"],
];
const SAML_FIELDS: [string, string, boolean?][] = [
  ["idp_metadata_url", "IdP metadata URL", true],
  ["idp_entity_id", "IdP entity ID", true],
  ["sso_url", "Sign-in URL", true],
  ["sp_entity_id", "SP entity ID", true],
  ["attribute_username", "Username attribute"],
  ["attribute_email", "Email attribute"],
  ["attribute_groups", "Groups attribute"],
];

function ProviderSheet({ provider, onClose }: { provider: Provider | null; onClose: () => void }) {
  const qc = useQueryClient();
  const [protocol, setProtocol] = useState<"oidc" | "saml">(
    provider?.protocol === "saml" ? "saml" : "oidc",
  );
  const fields = protocol === "saml" ? SAML_FIELDS : OIDC_FIELDS;
  const base: Form = { name: provider?.name ?? "" };
  for (const [k] of [...OIDC_FIELDS, ...SAML_FIELDS]) {
    const v = provider ? (provider as unknown as Record<string, unknown>)[k] : "";
    base[k] = typeof v === "string" ? v : "";
  }
  const [f, setF] = useState<Form>(base);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [testId, setTestId] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: async () => {
      const body: Record<string, unknown> = { protocol, name: f.name };
      for (const [k] of fields) {
        if (provider && f[k] === base[k]) continue;
        if (f[k] !== "" || provider) body[k] = f[k] === "" ? null : f[k];
      }
      if (provider)
        return unwrap(
          await api.PATCH("/api/v1/identity-providers/{idp}", {
            params: {
              path: { idp: provider.id },
              header: { "if-match": String(provider.version) },
            },
            body: body as never,
          }),
        );
      return unwrap(
        await api.POST("/api/v1/identity-providers", { body: { ...body, enabled: true } as never }),
      );
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "identity-providers"] });
      onClose();
    },
  });
  const del = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.DELETE("/api/v1/identity-providers/{idp}", {
          params: {
            path: { idp: provider!.id },
            header: { "if-match": String(provider!.version) },
          },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "identity-providers"] });
      onClose();
    },
  });
  const start = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/identity-providers/{idp}/test", {
          params: { path: { idp: provider!.id } },
          body: { return_to: null },
        }),
      ),
    onSuccess: (r) => {
      setTestId(r.test_id);
      window.open(r.authorize_url, "_blank", "noopener");
    },
  });
  const result = useQuery({
    queryKey: ["admin", "idp-test", provider?.id, testId],
    enabled: !!provider && !!testId,
    refetchInterval: (q) => (q.state.data?.status === "pending" ? 2000 : false),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/identity-providers/{idp}/test/{test_id}", {
          params: { path: { idp: provider!.id, test_id: testId! } },
        }),
      ),
  });
  const fieldErrors =
    save.error instanceof ApiError && save.error.status === 422
      ? Object.fromEntries(
          (save.error.problem?.errors ?? []).map((e) => [e.path.split(".")[0], e.message]),
        )
      : {};
  const dirty = provider ? Object.keys(base).some((k) => f[k] !== base[k]) : f.name !== "";
  const input = (k: string, label: string, mono = false) => (
    <label key={k} className="block space-y-1.5 text-sm font-medium">
      {t(label)}
      <input
        aria-label={t(label)}
        type={k === "client_secret" ? "password" : "text"}
        autoComplete="off"
        className={cn(INPUT, mono && "font-mono")}
        value={f[k] ?? ""}
        onChange={(e) => setF({ ...f, [k]: e.target.value })}
      />
      {fieldErrors[k] && (
        <span role="alert" className="block text-xs font-normal text-status-down">
          {fieldErrors[k]}
        </span>
      )}
    </label>
  );
  return (
    <>
      <Sheet
        open
        onOpenChange={(o) => !o && onClose()}
        title={provider ? t("Edit {name}", { name: provider.name }) : t("Add provider")}
        footer={
          <>
            {provider && (
              <Button variant="outline" onClick={() => setConfirmDelete(true)}>
                {t("Delete")}
              </Button>
            )}
            <Button variant="ghost" className="ml-auto" onClick={onClose}>
              {t("Cancel")}
            </Button>
            <Button disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
              {provider ? t("Save") : t("Add provider")}
            </Button>
          </>
        }
      >
        {save.error && Object.keys(fieldErrors).length === 0 && <ErrorState error={save.error} />}
        {!provider && (
          <div
            role="radiogroup"
            aria-label={t("Protocol")}
            className="inline-flex rounded-md border"
          >
            {(["oidc", "saml"] as const).map((k) => (
              <button
                key={k}
                type="button"
                role="radio"
                aria-checked={protocol === k}
                onClick={() => setProtocol(k)}
                className={cn(
                  "px-4 py-1.5 text-sm first:rounded-l-md last:rounded-r-md",
                  protocol === k ? "bg-accent font-medium" : "text-muted-foreground",
                )}
              >
                {k === "oidc" ? "OIDC" : "SAML 2.0"}
              </button>
            ))}
          </div>
        )}
        {input("name", "Name")}
        {fields.map(([k, label, mono]) => input(k, label, mono))}
        {provider && (provider.warnings?.length ?? 0) > 0 && (
          <ul className="space-y-1 rounded-md bg-muted p-3 text-xs">
            {provider.warnings.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        )}
        {provider && isSaml(provider) && (
          <p className="text-xs text-muted-foreground">
            {t("Give your IdP")}: <span className="font-mono">{provider.acs_url}</span> ·{" "}
            <a className="text-primary hover:underline" href={provider.sp_metadata_url}>
              {t("SP metadata")}
            </a>
          </p>
        )}
        {provider && (
          <section className="space-y-2 rounded-lg border p-3">
            <div className="flex items-center gap-3">
              <h3 className="text-sm font-medium">{t("Test sign-in")}</h3>
              <Button
                size="sm"
                variant="outline"
                disabled={start.isPending}
                onClick={() => start.mutate()}
              >
                {t("Start test")}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {t(
                "Opens the provider's sign-in in a new tab and shows what this person would get. Nothing is saved.",
              )}
            </p>
            {start.error && <ErrorState error={start.error} />}
            {result.data && (
              <div role="status" className="space-y-1 text-sm">
                <StatusPill
                  status={
                    result.data.error
                      ? "down"
                      : result.data.status === "done"
                        ? "healthy"
                        : "pending"
                  }
                >
                  {result.data.status}
                </StatusPill>
                {result.data.error && <p className="text-status-down">{result.data.error}</p>}
                {result.data.username && <p>{result.data.username}</p>}
                {result.data.groups && (
                  <p className="font-mono text-xs">{result.data.groups.join(", ") || "—"}</p>
                )}
                {result.data.would_grant?.map((g, i) => (
                  <p key={i} className="text-xs text-muted-foreground">
                    {JSON.stringify(g)}
                  </p>
                ))}
              </div>
            )}
          </section>
        )}
      </Sheet>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete {name}?", { name: provider?.name ?? "" })}
        confirmLabel={t("Delete provider")}
        busy={del.isPending}
        onConfirm={async () => {
          try {
            await del.mutateAsync();
          } finally {
            setConfirmDelete(false);
          }
        }}
      >
        <p>
          {t(
            "People who sign in through it lose access and its role mappings stop matching. This acts immediately and is audited.",
          )}
        </p>
      </ConfirmDialog>
    </>
  );
}

function BreakGlass() {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["admin", "break-glass"],
    refetchInterval: 30_000,
    queryFn: async () => unwrap(await api.GET("/api/v1/system/break-glass")),
  });
  const [reason, setReason] = useState("");
  const [ttl, setTtl] = useState("240");
  const [enrol, setEnrol] = useState<components["schemas"]["BreakGlassEnrollment"] | null>(null);
  const [totp, setTotp] = useState("");
  const [stored, setStored] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);
  const refresh = () => void qc.invalidateQueries({ queryKey: ["admin", "break-glass"] });
  const sw = useMutation({
    mutationFn: async (on: boolean) =>
      unwrap(
        await api.PUT("/api/v1/system/break-glass", {
          body: on
            ? { enabled: true, reason: reason || null, ttl_minutes: Number(ttl) || null }
            : { enabled: false },
        }),
      ),
    onSuccess: refresh,
  });
  const enrolAdmin = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/system/break-glass/admin", {
          body: { username: status.data?.admin?.username ?? "breakglass" },
        }),
      ),
    gcTime: 0,
    onSuccess: (r) => {
      setEnrol(r);
      // The password and authenticator secret live in component state only while the dialog is open.
      setTimeout(() => enrolAdmin.reset(), 0);
    },
  });
  const confirm = useMutation({
    mutationFn: async () =>
      unwrap(await api.POST("/api/v1/system/break-glass/admin/confirm", { body: { totp } })),
    onSuccess: () => {
      setEnrol(null);
      setStored(false);
      setTotp("");
      refresh();
    },
  });
  if (status.isPending) return <TableSkeleton rows={2} cols={2} />;
  if (status.isError)
    return <ErrorState error={status.error} onRetry={() => void status.refetch()} />;
  const b = status.data;
  const left = b.enabled_until
    ? Math.max(0, new Date(b.enabled_until).getTime() - Math.max(now, status.dataUpdatedAt))
    : null;
  const leftText =
    left == null
      ? null
      : t("{h} h {m} min left", {
          h: Math.floor(left / 3_600_000),
          m: Math.floor((left % 3_600_000) / 60_000),
        });
  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <header className="flex items-center gap-3 border-b px-4 py-3">
        <h2 className="text-base font-semibold">{t("Break-glass administrator")}</h2>
        {b.enabled ? (
          <StatusPill status="down">
            {t("On")}
            {leftText ? ` · ${leftText}` : ""}
          </StatusPill>
        ) : (
          <StatusPill status="neutral">{t("Off")}</StatusPill>
        )}
      </header>
      <div className="space-y-3 p-4 text-sm">
        <p className="text-muted-foreground">
          {t(
            "A local system-admin with a password and TOTP, for when no identity provider works. Sessions last at most 1 hour (15 minutes idle); every attempt is audited.",
          )}
        </p>
        <dl className="grid grid-cols-[8rem_1fr] gap-y-2">
          <dt className="text-muted-foreground">{t("Administrator")}</dt>
          <dd>
            {b.admin ? (
              <>
                <span className="font-mono">{b.admin.username}</span>
                {" · "}
                {b.admin.totp_confirmed ? t("TOTP confirmed") : t("TOTP not confirmed")}
                {" · "}
                {t("created {date}", { date: new Date(b.admin.created_at).toLocaleDateString() })}
              </>
            ) : (
              t("none yet")
            )}
          </dd>
          <dt className="text-muted-foreground">{t("Last sign-in")}</dt>
          <dd>
            {b.admin?.last_login_at ? new Date(b.admin.last_login_at).toLocaleString() : t("never")}
          </dd>
          {b.enabled && (
            <>
              <dt className="text-muted-foreground">{t("Enabled")}</dt>
              <dd>
                {b.enabled_until
                  ? t("until {time}", { time: new Date(b.enabled_until).toLocaleTimeString() })
                  : ""}
                {b.enabled_by ? ` ${t("by {who}", { who: b.enabled_by })}` : ""}
                {b.reason ? ` · “${b.reason}”` : ""}
                {!b.enabled_until &&
                  !b.enabled_by &&
                  !b.reason &&
                  t("Always on (set by the deployment)")}
              </dd>
            </>
          )}
        </dl>
        {b.forced_by_deployment && (
          <p className="text-xs text-muted-foreground">
            {t("This deployment forces break-glass on; it cannot be turned off here.")}
          </p>
        )}
        {(sw.error || enrolAdmin.error) && <ErrorState error={sw.error ?? enrolAdmin.error} />}
        {!b.enabled && !b.forced_by_deployment && (
          <div className="flex flex-wrap items-end gap-3">
            <label className="space-y-1.5 font-medium">
              {t("Reason")}
              <input
                aria-label={t("Reason")}
                className={cn(INPUT, "w-72")}
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            <label className="space-y-1.5 font-medium">
              {t("Minutes")}
              <input
                aria-label={t("Minutes")}
                type="number"
                className={cn(INPUT, "w-28")}
                value={ttl}
                onChange={(e) => setTtl(e.target.value)}
              />
            </label>
            <Button disabled={!reason || sw.isPending} onClick={() => sw.mutate(true)}>
              {t("Turn on")}
            </Button>
          </div>
        )}
        <div className="flex items-center justify-between">
          {b.enabled && !b.forced_by_deployment ? (
            <Button variant="outline" disabled={sw.isPending} onClick={() => sw.mutate(false)}>
              {t("Turn off now")}
            </Button>
          ) : (
            <span />
          )}
          <Button
            variant="ghost"
            disabled={enrolAdmin.isPending}
            onClick={() => enrolAdmin.mutate()}
          >
            {b.admin ? t("Replace administrator") : t("Create administrator")}
          </Button>
        </div>
      </div>
      {enrol && (
        <Sheet
          open
          centered
          onOpenChange={(o) => !o && setEnrol(null)}
          title={t("Set up the administrator")}
          footer={
            <>
              <Button variant="ghost" className="ml-auto" onClick={() => setEnrol(null)}>
                {t("Cancel")}
              </Button>
              <Button
                disabled={!stored || totp.length < 6 || confirm.isPending}
                onClick={() => confirm.mutate()}
              >
                {t("Confirm")}
              </Button>
            </>
          }
        >
          <ShowOnceSecret
            secrets={[
              { label: t("Username"), value: enrol.username },
              { label: t("Password"), value: enrol.password },
              { label: t("TOTP secret"), value: enrol.totp_secret },
            ]}
            qrUri={enrol.otpauth_uri}
            stored={stored}
            onStoredChange={setStored}
            note={t(
              "Shown once. Store the password and add the secret to an authenticator app (scan the code), then enter a code below.",
            )}
          />
          {confirm.error && <ErrorState error={confirm.error} />}
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Authenticator code")}
            <input
              aria-label={t("Authenticator code")}
              inputMode="numeric"
              autoComplete="off"
              className={INPUT}
              value={totp}
              onChange={(e) => setTotp(e.target.value)}
            />
          </label>
        </Sheet>
      )}
    </section>
  );
}

export function IdentityProviders() {
  const qc = useQueryClient();
  const list = useProviderList();
  const mappings = useQuery({
    queryKey: ["admin", "role-mappings"],
    queryFn: async () => unwrap(await api.GET("/api/v1/role-mappings")),
  });
  const [editing, setEditing] = useState<Provider | "new" | null>(null);
  const toggle = useMutation({
    mutationFn: async (p: Provider) =>
      unwrap(
        await api.PATCH("/api/v1/identity-providers/{idp}", {
          params: { path: { idp: p.id }, header: { "if-match": String(p.version) } },
          body: { protocol: p.protocol, enabled: !p.enabled } as never,
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["admin", "identity-providers"] }),
  });
  const rows = (list.data ?? []) as Provider[];
  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("Identity providers")}</h1>
          <p className="text-sm text-muted-foreground">
            {t(
              "Every enabled provider appears on the login page. Changes are immediate and audited.",
            )}
          </p>
        </div>
        <Button onClick={() => setEditing("new")}>{t("Add provider")}</Button>
      </div>
      {toggle.error && <ErrorState error={toggle.error} />}
      <div className="overflow-hidden rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={5} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState
            title={t("No identity providers")}
            body={t("Add an OIDC or SAML provider so people can sign in.")}
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["Name", "Protocol", "Issuer or IdP entity", "Role mappings", "Status", ""].map(
                  (h, i) => (
                    <th key={i} className="px-4 py-2.5 font-medium">
                      {t(h)}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((p) => (
                <tr key={p.id}>
                  <td className="px-4 py-3">
                    <button
                      type="button"
                      className="font-medium text-primary hover:underline"
                      onClick={() => setEditing(p)}
                    >
                      {p.name}
                    </button>
                  </td>
                  <td className="px-4 py-3">{isSaml(p) ? "SAML 2.0" : "OIDC"}</td>
                  <td className="px-4 py-3 font-mono text-xs">
                    {isSaml(p) ? p.idp_entity_id : (p as Oidc).issuer_url}
                  </td>
                  <td className="px-4 py-3">{mappingSummary(mappings.data ?? [], p.id)}</td>
                  <td className="px-4 py-3">
                    {p.enabled ? (
                      <StatusPill status="healthy">{t("Enabled")}</StatusPill>
                    ) : (
                      <StatusPill status="neutral">{t("Disabled")}</StatusPill>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={toggle.isPending}
                      onClick={() => toggle.mutate(p)}
                    >
                      {p.enabled ? t("Disable") : t("Enable")}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <BreakGlass />
      {editing && (
        <ProviderSheet
          key={editing === "new" ? "new" : `${editing.id}-${editing.version}`}
          provider={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}
