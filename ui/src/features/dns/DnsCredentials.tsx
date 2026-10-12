/**
 * Tenant DNS credentials (tenant-admin): the tenant's own DNS provider accounts, used for DNS-01
 * validation. One schema-driven sheet: pick a provider, then fill that provider's fields. Secrets
 * are write-only (stored in OpenBao). Saved live (no change set) and audited.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, RefreshCw, XCircle } from "lucide-react";
import { useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { ChipInput, validateHostname } from "@/components/sy/ChipInput";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { WriteOnlySecret } from "@/components/sy/WriteOnlySecret";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Tip } from "@/components/ui/tooltip";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Cred = components["schemas"]["DnsProviderRead"];
type ProviderType = components["schemas"]["DnsProviderTypeRead"];
type TestRead = components["schemas"]["DnsProviderTestRead"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

function useCreds(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "dns-providers"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/dns-providers", { params: { path: { t: tenant } } }),
      ),
  });
}
function useTypes() {
  return useQuery({
    queryKey: ["dns-provider-types"],
    staleTime: 300_000,
    queryFn: async () => unwrap(await api.GET("/api/v1/dns-provider-types")),
  });
}

/** Zone names for the chip input: a domain, optionally under a wildcard-free name. */
export function validateZone(text: string): string | null {
  return validateHostname(text.replace(/^\*\./, ""))
    ? t("Use a domain such as example.com.")
    : null;
}

function CredentialSheet({
  tenant,
  cred,
  types,
  onClose,
}: {
  tenant: string;
  cred: Cred | null;
  types: ProviderType[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = cred !== null;
  const [type, setType] = useState(cred?.type ?? types[0]?.type ?? "");
  const [name, setName] = useState(cred?.name ?? "");
  const [zones, setZones] = useState<string[]>(cred?.zones ?? []);
  const [enabled] = useState(cred?.enabled ?? true);
  const [config, setConfig] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(cred?.config ?? {}).map(([k, v]) => [k, String(v ?? "")])),
  );
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<unknown>(null);
  const [test, setTest] = useState<{ at: Date; r: TestRead } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const spec = types.find((x) => x.type === type);

  function changeType(next: string) {
    setType(next);
    setConfig({});
    setSecrets({});
  }
  function fail(e: unknown) {
    if (e instanceof ApiError && e.status === 422) {
      const m: Record<string, string> = {};
      for (const x of e.problem?.errors ?? []) {
        const key = x.path.replace(/^(config|credentials)\./, "").split(".")[0] ?? x.path;
        m[key] = x.message;
      }
      setErrors(m);
    } else setProblem(e);
  }

  const save = useMutation({
    mutationFn: async () => {
      setErrors({});
      setProblem(null);
      const cfg: Record<string, string> = {};
      const sec: Record<string, string> = {};
      for (const f of spec?.fields ?? []) {
        if (f.secret) {
          if (secrets[f.key]) sec[f.key] = secrets[f.key]!;
        } else if (config[f.key] !== undefined && config[f.key] !== "") cfg[f.key] = config[f.key]!;
      }
      if (!cred)
        return unwrap(
          await api.POST("/api/v1/tenants/{t}/dns-providers", {
            params: { path: { t: tenant } },
            body: { name, type, zones, enabled, config: cfg, credentials: sec },
          }),
        );
      return unwrap(
        await api.PATCH("/api/v1/tenants/{t}/dns-providers/{provider}", {
          params: { path: { t: tenant, provider: cred.id } },
          body: {
            zones,
            enabled,
            config: cfg,
            credentials: Object.keys(sec).length > 0 ? sec : null,
          },
        }),
      );
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["t", tenant, "dns-providers"] });
      onClose();
    },
    onError: fail,
  });

  const runTest = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/dns-providers/{provider}/test", {
          params: { path: { t: tenant, provider: cred!.id } },
          body: {},
        }),
      ),
    onSuccess: (r) => setTest({ at: new Date(), r }),
    onError: setProblem,
  });

  const remove = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.DELETE("/api/v1/tenants/{t}/dns-providers/{provider}", {
          params: { path: { t: tenant, provider: cred!.id } },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["t", tenant, "dns-providers"] });
      onClose();
    },
    onError: (e) => {
      setConfirmDelete(false);
      setProblem(e);
    },
  });

  const missing = (spec?.fields ?? []).some(
    (f) => f.required && (f.secret ? !editing && !secrets[f.key] : !(config[f.key] || f.default)),
  );
  const valid = name !== "" && type !== "" && !missing;

  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={editing ? t("Edit {name}", { name: cred.name }) : t("Add DNS credential")}
      badge={<StatusPill status="pending">{t("Live")}</StatusPill>}
      footer={
        <>
          {editing && (
            <Button
              variant="ghost"
              disabled={remove.isPending}
              onClick={() => setConfirmDelete(true)}
            >
              {t("Delete")}
            </Button>
          )}
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button disabled={!valid || save.isPending} onClick={() => save.mutate()}>
            {editing ? t("Save") : t("Add credential")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      <fieldset className="space-y-2" disabled={editing}>
        <legend className="text-sm font-medium">{t("Provider")}</legend>
        <div className="grid grid-cols-3 gap-2">
          {types.map((x) => (
            <label
              key={x.type}
              className={cn(
                "flex cursor-pointer items-start gap-2 rounded-lg border bg-card p-2.5 text-sm",
                type === x.type && "border-primary bg-primary/10",
                editing && type !== x.type && "opacity-60",
              )}
            >
              <input
                type="radio"
                name="dns-type"
                aria-label={x.label}
                className="mt-1"
                checked={type === x.type}
                onChange={() => changeType(x.type)}
              />
              <span className="font-medium">{x.label}</span>
            </label>
          ))}
        </div>
      </fieldset>
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Name")}
        <input
          aria-label={t("Name")}
          className={INPUT}
          disabled={editing}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <span className="block text-xs font-normal text-muted-foreground">
          {t("Shown to operators in the certificate request.")}
        </span>
        {editing && (
          <span className="block text-xs font-normal text-muted-foreground">
            {t("The provider of a saved credential can't change; add a new one instead.")}
          </span>
        )}
        {errors.name && (
          <span role="alert" className="block text-xs font-normal text-status-down">
            {errors.name}
          </span>
        )}
      </label>
      {(spec?.fields ?? []).map((f) =>
        f.secret ? (
          <div key={f.key} className="space-y-1">
            <WriteOnlySecret
              label={f.label}
              isSet={editing && cred.credentials_set}
              value={secrets[f.key] ?? ""}
              onChange={(v) => setSecrets({ ...secrets, [f.key]: v })}
              multiline={f.kind === "textarea"}
              help={f.help || undefined}
              required={f.required}
            />
            {errors[f.key] && (
              <p role="alert" className="text-xs text-status-down">
                {errors[f.key]}
              </p>
            )}
          </div>
        ) : (
          <label key={f.key} className="block space-y-1.5 text-sm font-medium">
            {f.required ? f.label : t("{label} (optional)", { label: f.label })}
            {f.kind === "select" ? (
              <select
                aria-label={f.label}
                className={INPUT}
                value={config[f.key] ?? f.default ?? ""}
                onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })}
              >
                {!f.required && <option value="">{t("Default")}</option>}
                {f.options.map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            ) : f.kind === "textarea" ? (
              <textarea
                aria-label={f.label}
                className={cn(INPUT, "h-20 py-2 font-mono text-xs")}
                value={config[f.key] ?? ""}
                onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })}
              />
            ) : (
              <input
                aria-label={f.label}
                className={cn(INPUT, "font-mono")}
                placeholder={f.default ?? undefined}
                value={config[f.key] ?? ""}
                onChange={(e) => setConfig({ ...config, [f.key]: e.target.value })}
              />
            )}
            {f.help && (
              <span className="block text-xs font-normal text-muted-foreground">{f.help}</span>
            )}
            {errors[f.key] && (
              <span role="alert" className="block text-xs font-normal text-status-down">
                {errors[f.key]}
              </span>
            )}
          </label>
        ),
      )}
      <div className="space-y-1.5">
        <div className="text-sm font-medium">{t("Zones this credential covers")}</div>
        <ChipInput
          label={t("Zones this credential covers")}
          value={zones}
          onChange={setZones}
          freeText={{ validate: validateZone }}
          placeholder={t("Add a zone")}
          error={errors.zones}
        />
        <p className="text-xs text-muted-foreground">
          {t("Names in these zones and their subdomains are validated with this credential.")}
          {spec?.supports_zone_discovery
            ? ` ${t("This provider finds the zone of a name itself; zones only choose between your credentials.")}`
            : ""}
        </p>
        {errors.zones && (
          <p role="alert" className="text-xs text-status-down">
            {errors.zones}
          </p>
        )}
      </div>
      {editing && (
        <div className="space-y-2">
          <div className="flex items-start gap-3">
            <Button variant="outline" disabled={runTest.isPending} onClick={() => runTest.mutate()}>
              <RefreshCw
                className={cn("size-4", runTest.isPending && "animate-spin")}
                aria-hidden
              />{" "}
              {t("Test")}
            </Button>
            <p className="text-xs text-muted-foreground">
              {t(
                "Creates a short-lived _switchyard-test TXT record in each zone, waits until public resolvers see it, then deletes it. Saved settings only.",
              )}
            </p>
          </div>
          {test && (
            <div
              role="status"
              aria-label={t("Test result")}
              className="space-y-1.5 rounded-lg border p-3 text-sm"
            >
              <div className="font-medium">
                {test.r.ok ? t("Test passed") : t("Test failed")} · {test.at.toLocaleTimeString()}
              </div>
              {test.r.zones.map((z) => (
                <div key={z.zone} className="flex items-start gap-2">
                  {z.ok ? (
                    <CheckCircle2
                      className="mt-0.5 size-4 shrink-0 text-status-healthy"
                      aria-hidden
                    />
                  ) : (
                    <XCircle className="mt-0.5 size-4 shrink-0 text-status-down" aria-hidden />
                  )}
                  <span>
                    <span className="font-mono">{z.zone}</span>: {z.detail}
                  </span>
                </div>
              ))}
              {!test.r.ok && test.r.zones.length === 0 && (
                <p className="text-xs">{test.r.detail}</p>
              )}
            </div>
          )}
        </div>
      )}
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete {name}?", { name: cred?.name ?? "" })}
        confirmLabel={t("Delete credential")}
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
      >
        <p>
          {t(
            "The credential and its stored secrets are removed. This acts immediately, outside the change set, and is audited. Certificates that use it can't renew until you add another.",
          )}
        </p>
      </ConfirmDialog>
    </Sheet>
  );
}

export function DnsCredentials({ tenant }: { tenant: string }) {
  const list = useCreds(tenant);
  const types = useTypes();
  const can = useCan(tenant);
  const manage = can("manage", "dns_provider");
  const [sheet, setSheet] = useState<Cred | "new" | null>(null);
  const items = list.data ?? [];
  const label = (k: string) => types.data?.find((x) => x.type === k)?.label ?? k;
  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("DNS credentials")}</h1>
          <p className="text-sm text-muted-foreground">
            {t(
              "{tenant}'s own DNS provider accounts, used for DNS-01 validation when a certificate is requested or renewed.",
              { tenant },
            )}
          </p>
        </div>
        <Tip label={manage.reason}>
          <Button disabled={!manage.allowed || !types.data} onClick={() => setSheet("new")}>
            {t("Add credential")}
          </Button>
        </Tip>
      </div>
      <div className="overflow-x-auto rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={5} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState
            title={t("No DNS credentials yet")}
            body={t("Add one to request certificates with DNS-01, including wildcards.")}
            action={
              manage.allowed ? (
                <Button onClick={() => setSheet("new")}>{t("Add credential")}</Button>
              ) : undefined
            }
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["Name", "Provider", "Zones", "Status"].map((h) => (
                  <th key={h} className="px-4 py-2.5 font-medium">
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
                  <td className="px-4 py-3">{label(c.type)}</td>
                  <td className="px-4 py-3 font-mono text-xs break-all">
                    {c.zones.length ? (
                      c.zones.join(", ")
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {c.enabled ? (
                      <StatusPill status="healthy">{t("Enabled")}</StatusPill>
                    ) : (
                      <StatusPill status="neutral">{t("Disabled")}</StatusPill>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {t(
          "Tenant-admins manage this page; changes apply at once (no change set) and are audited. Operators requesting a certificate get the credential whose zone is the longest match for each name; two credentials can't claim the same zone.",
        )}
      </p>
      {sheet !== null && types.data && (
        <CredentialSheet
          key={sheet === "new" ? "new" : sheet.id}
          tenant={tenant}
          cred={sheet === "new" ? null : sheet}
          types={types.data}
          onClose={() => setSheet(null)}
        />
      )}
    </div>
  );
}
