// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Admin: ACME providers (system-admin). A provider is a certificate authority and the account the
 * controller uses with it, shared by every tenant. Saved live (no change set) and audited;
 * keys and secrets are write-only and stored in OpenBao.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, RefreshCw, XCircle } from "lucide-react";
import { useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { WriteOnlySecret } from "@/components/sy/WriteOnlySecret";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Provider = components["schemas"]["AcmeProviderRead"];
type Preset = components["schemas"]["AcmePresetRead"];
type TestResult = components["schemas"]["AcmeProviderTestRead"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

function useProviders() {
  return useQuery({
    queryKey: ["admin", "acme-providers"],
    queryFn: async () => unwrap(await api.GET("/api/v1/acme-providers")),
  });
}
function usePresets() {
  return useQuery({
    queryKey: ["admin", "acme-presets"],
    queryFn: async () => unwrap(await api.GET("/api/v1/acme-provider-presets")),
  });
}

function Field({
  label,
  error,
  help,
  children,
}: {
  label: string;
  error?: string;
  help?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block space-y-1.5 text-sm font-medium">
      {label}
      {children}
      {help && <span className="block text-xs font-normal text-muted-foreground">{help}</span>}
      {error && (
        <span role="alert" className="block text-xs font-normal text-status-down">
          {error}
        </span>
      )}
    </label>
  );
}

/** Account status for the list: not tested yet, registered, or switched off. */
function AccountPill({ p }: { p: Provider }) {
  if (!p.enabled) return <StatusPill status="neutral">{t("Disabled")}</StatusPill>;
  return p.account_registered ? (
    <StatusPill status="healthy">{t("Registered")}</StatusPill>
  ) : (
    <StatusPill status="degraded">{t("Not registered")}</StatusPill>
  );
}

interface Form {
  preset: string;
  name: string;
  directory_url: string;
  email: string;
  account_key: string;
  eab_kid: string;
  eab_hmac_key: string;
  auth_mode: "none" | "eab" | "oauth";
  token_url: string;
  client_id: string;
  oauth_client_secret: string;
  scope: string;
  audience: string;
  eab_url: string;
  bearer_on_acme: boolean;
  ca_bundle_pem: string;
  default: boolean;
  enabled: boolean;
  terms_agreed: boolean;
}

function toForm(p: Provider | null, preset?: Preset): Form {
  return {
    preset: p?.preset ?? preset?.key ?? "custom",
    name: p?.name ?? "",
    directory_url: p?.directory_url ?? preset?.directory_url ?? "",
    email: p?.email ?? "",
    account_key: "",
    eab_kid: p?.eab_kid ?? "",
    eab_hmac_key: "",
    auth_mode: p?.auth_mode ?? preset?.auth_mode ?? "none",
    token_url: p?.oauth?.token_url ?? "",
    client_id: p?.oauth?.client_id ?? "",
    oauth_client_secret: "",
    scope: p?.oauth?.scope ?? "",
    audience: p?.oauth?.audience ?? "",
    eab_url: p?.oauth?.eab_url ?? "",
    bearer_on_acme: p?.oauth?.bearer_on_acme ?? false,
    ca_bundle_pem: "",
    default: p?.default ?? false,
    enabled: p?.enabled ?? true,
    terms_agreed: p?.terms_agreed ?? false,
  };
}

function ProviderSheet({
  provider,
  presets,
  defaultName,
  onClose,
}: {
  provider: Provider | null;
  presets: Preset[];
  /** Name of the provider that is the default now, if any. */
  defaultName?: string;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = provider !== null;
  const [f, setF] = useState<Form>(() =>
    toForm(provider, presets.find((p) => p.key === "letsencrypt") ?? presets[0]),
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<unknown>(null);
  const [test, setTest] = useState<{ at: Date; r: TestResult } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const set = (patch: Partial<Form>) => setF({ ...f, ...patch });
  const preset = presets.find((p) => p.key === f.preset);
  const eab = f.auth_mode === "eab";
  const oauth = f.auth_mode === "oauth";

  function pickPreset(p: Preset) {
    setF({
      ...f,
      preset: p.key,
      auth_mode: p.auth_mode,
      directory_url: p.directory_url ?? "",
      name: f.name || p.key.replace(/_/g, "-"),
    });
  }

  function fail(e: unknown) {
    if (e instanceof ApiError && e.status === 422) {
      const m: Record<string, string> = {};
      for (const x of e.problem?.errors ?? []) m[x.path.split(".")[0] ?? x.path] = x.message;
      setErrors(m);
    } else setProblem(e);
  }

  const save = useMutation({
    mutationFn: async () => {
      setErrors({});
      setProblem(null);
      const oauthBody = oauth
        ? {
            token_url: f.token_url || null,
            client_id: f.client_id || null,
            scope: f.scope || null,
            audience: f.audience || null,
            eab_url: f.eab_url || null,
            bearer_on_acme: f.bearer_on_acme,
          }
        : undefined;
      if (!provider)
        return unwrap(
          await api.POST("/api/v1/acme-providers", {
            body: {
              name: f.name,
              preset: f.preset,
              directory_url: f.directory_url || null,
              email: f.email || null,
              auth_mode: f.auth_mode,
              account_key: f.account_key || null,
              eab_kid: eab ? f.eab_kid || null : null,
              eab_hmac_key: eab ? f.eab_hmac_key || null : null,
              oauth: oauthBody,
              oauth_client_secret: oauth ? f.oauth_client_secret || null : null,
              ca_bundle_pem: f.ca_bundle_pem || null,
              default: f.default,
              enabled: f.enabled,
              terms_agreed: f.terms_agreed,
            },
          }),
        );
      const patch: Record<string, unknown> = {};
      if (f.directory_url !== provider.directory_url) patch.directory_url = f.directory_url;
      if (f.email !== (provider.email ?? "")) patch.email = f.email;
      if (f.auth_mode !== provider.auth_mode) patch.auth_mode = f.auth_mode;
      if (f.account_key) patch.account_key = f.account_key;
      if (eab && f.eab_kid !== (provider.eab_kid ?? "")) patch.eab_kid = f.eab_kid;
      if (f.eab_hmac_key) patch.eab_hmac_key = f.eab_hmac_key;
      if (oauthBody) patch.oauth = oauthBody;
      if (f.oauth_client_secret) patch.oauth_client_secret = f.oauth_client_secret;
      if (f.ca_bundle_pem) patch.ca_bundle_pem = f.ca_bundle_pem;
      if (f.default !== provider.default) patch.default = f.default;
      if (f.enabled !== provider.enabled) patch.enabled = f.enabled;
      return unwrap(
        await api.PATCH("/api/v1/acme-providers/{provider}", {
          params: { path: { provider: provider.id } },
          body: patch,
        }),
      );
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "acme-providers"] });
      onClose();
    },
    onError: fail,
  });

  const runTest = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/acme-providers/{provider}/test", {
          params: { path: { provider: provider!.id } },
        }),
      ),
    onSuccess: (r) => {
      setTest({ at: new Date(), r });
      void qc.invalidateQueries({ queryKey: ["admin", "acme-providers"] });
    },
    onError: setProblem,
  });

  const remove = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.DELETE("/api/v1/acme-providers/{provider}", {
          params: { path: { provider: provider!.id } },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "acme-providers"] });
      onClose();
    },
    onError: (e) => {
      setConfirmDelete(false);
      setProblem(e);
    },
  });

  const usedBy = provider?.used_by ?? 0;
  const valid =
    f.name !== "" &&
    f.directory_url !== "" &&
    (editing || f.terms_agreed) &&
    (!eab || editing || (f.eab_kid !== "" && f.eab_hmac_key !== ""));

  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={editing ? t("Edit {name}", { name: provider.name }) : t("Add ACME provider")}
      badge={<StatusPill status="pending">{t("Live")}</StatusPill>}
      footer={
        <>
          {editing && (
            <Button
              variant="ghost"
              disabled={usedBy > 0 || remove.isPending}
              onClick={() => setConfirmDelete(true)}
            >
              {t("Delete")}
            </Button>
          )}
          {editing && (
            <span className="text-xs text-muted-foreground">
              {usedBy > 0
                ? t("Used by {n} certificates, so it cannot be deleted", { n: usedBy })
                : t("Used by no certificates")}
            </span>
          )}
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button disabled={!valid || save.isPending} onClick={() => save.mutate()}>
            {editing ? t("Save") : t("Add provider")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      <fieldset className="space-y-2" disabled={editing}>
        <legend className="text-sm font-medium">{t("Type")}</legend>
        <div className="grid grid-cols-2 gap-2">
          {presets.map((p) => (
            <label
              key={p.key}
              className={cn(
                "flex cursor-pointer items-start gap-2.5 rounded-lg border bg-card p-3 text-sm",
                f.preset === p.key && "border-primary bg-primary/10",
                editing && f.preset !== p.key && "opacity-60",
              )}
            >
              <input
                type="radio"
                name="acme-preset"
                aria-label={p.label}
                className="mt-1"
                checked={f.preset === p.key}
                onChange={() => pickPreset(p)}
              />
              <span className="flex flex-col gap-0.5">
                <span className="font-medium">{p.label}</span>
                <span className="text-xs text-muted-foreground">{p.note}</span>
              </span>
            </label>
          ))}
        </div>
        {editing && (
          <p className="text-xs text-muted-foreground">
            {t("The type of a saved provider can't change; add a new one instead.")}
          </p>
        )}
      </fieldset>
      <Field label={t("Name")} error={errors.name}>
        <input
          aria-label={t("Name")}
          className={INPUT}
          disabled={editing}
          value={f.name}
          onChange={(e) => set({ name: e.target.value })}
        />
      </Field>
      <Field
        label={t("Directory URL")}
        error={errors.directory_url}
        help={preset?.directory_url ? undefined : t("Required: this type has no fixed directory.")}
      >
        <input
          aria-label={t("Directory URL")}
          className={cn(INPUT, "font-mono")}
          value={f.directory_url}
          onChange={(e) => set({ directory_url: e.target.value })}
        />
      </Field>
      <Field label={t("Contact email")} error={errors.email}>
        <input
          aria-label={t("Contact email")}
          type="email"
          className={INPUT}
          value={f.email}
          onChange={(e) => set({ email: e.target.value })}
        />
      </Field>
      {editing ? (
        <WriteOnlySecret
          label={t("Account key")}
          isSet
          value={f.account_key}
          onChange={(v) => set({ account_key: v })}
          multiline
          help={
            provider.account_registered
              ? t("The account is registered with the CA. Replacing the key registers it again.")
              : t("The account is not registered yet; run Test connection.")
          }
        />
      ) : (
        <WriteOnlySecret
          label={t("Account key (optional)")}
          isSet={false}
          value={f.account_key}
          onChange={(v) => set({ account_key: v })}
          multiline
          help={t(
            "Leave empty and the controller generates an RSA 2048 key and keeps it in OpenBao.",
          )}
        />
      )}
      <fieldset className="space-y-1.5">
        <legend className="text-sm font-medium">{t("Authentication")}</legend>
        <div role="radiogroup" className="flex gap-4 text-sm">
          {(
            [
              ["none", "None"],
              ["eab", "External account binding"],
              ["oauth", "OAuth"],
            ] as const
          ).map(([k, label]) => (
            <label key={k} className="flex items-center gap-1.5">
              <input
                type="radio"
                name="acme-auth"
                checked={f.auth_mode === k}
                onChange={() => set({ auth_mode: k })}
              />
              {t(label)}
            </label>
          ))}
        </div>
      </fieldset>
      {eab && (
        <>
          <Field
            label={t("EAB key ID")}
            error={errors.eab_kid}
            help={
              preset?.eab_required
                ? t(
                    "{ca} requires External Account Binding. Find the key ID and HMAC in its settings.",
                    {
                      ca: preset.label,
                    },
                  )
                : undefined
            }
          >
            <input
              aria-label={t("EAB key ID")}
              className={cn(INPUT, "font-mono")}
              value={f.eab_kid}
              onChange={(e) => set({ eab_kid: e.target.value })}
            />
          </Field>
          <WriteOnlySecret
            label={t("EAB HMAC key")}
            isSet={provider?.eab_configured ?? false}
            value={f.eab_hmac_key}
            onChange={(v) => set({ eab_hmac_key: v })}
            required
          />
        </>
      )}
      {oauth && (
        <div className="space-y-3 rounded-lg border p-3">
          <p className="text-xs text-muted-foreground">
            {t(
              "The controller signs in with the client-credentials grant. Set an EAB endpoint, send the token on ACME requests, or both.",
            )}
          </p>
          <Field label={t("Token URL")}>
            <input
              aria-label={t("Token URL")}
              className={cn(INPUT, "font-mono")}
              value={f.token_url}
              onChange={(e) => set({ token_url: e.target.value })}
            />
          </Field>
          <Field label={t("Client ID")}>
            <input
              aria-label={t("Client ID")}
              className={cn(INPUT, "font-mono")}
              value={f.client_id}
              onChange={(e) => set({ client_id: e.target.value })}
            />
          </Field>
          <WriteOnlySecret
            label={t("Client secret")}
            isSet={provider?.oauth_secret_set ?? false}
            value={f.oauth_client_secret}
            onChange={(v) => set({ oauth_client_secret: v })}
          />
          <div className="grid grid-cols-2 gap-3">
            <Field label={t("Scope (optional)")}>
              <input
                aria-label={t("Scope")}
                className={cn(INPUT, "font-mono")}
                value={f.scope}
                onChange={(e) => set({ scope: e.target.value })}
              />
            </Field>
            <Field label={t("Audience (optional)")}>
              <input
                aria-label={t("Audience")}
                className={cn(INPUT, "font-mono")}
                value={f.audience}
                onChange={(e) => set({ audience: e.target.value })}
              />
            </Field>
          </div>
          <Field
            label={t("EAB endpoint (optional)")}
            help={t("Returns a fresh EAB key when called with the bearer token.")}
          >
            <input
              aria-label={t("EAB endpoint")}
              className={cn(INPUT, "font-mono")}
              value={f.eab_url}
              onChange={(e) => set({ eab_url: e.target.value })}
            />
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <Switch
              aria-label={t("Send the token on every ACME request")}
              checked={f.bearer_on_acme}
              onCheckedChange={(v) => set({ bearer_on_acme: v })}
            />
            {t("Send the token on every ACME request")}
          </label>
          {errors.oauth && (
            <p role="alert" className="text-xs text-status-down">
              {errors.oauth}
            </p>
          )}
        </div>
      )}
      <Field
        label={t("Trusted root certificates (optional)")}
        help={
          provider?.ca_bundle_set
            ? t("Extra roots are set. Paste PEM to replace them.")
            : t("PEM of the roots to trust for a private CA. Public data.")
        }
        error={errors.ca_bundle_pem}
      >
        <textarea
          aria-label={t("Trusted root certificates")}
          className={cn(INPUT, "h-24 py-2 font-mono text-xs")}
          value={f.ca_bundle_pem}
          onChange={(e) => set({ ca_bundle_pem: e.target.value })}
        />
      </Field>
      <div className="flex items-start gap-3">
        <div className="flex-1 text-sm">
          <div className="font-medium">{t("Default provider")}</div>
          <p className="text-xs text-muted-foreground">
            {f.default
              ? t("Certificates that name no provider use this one.")
              : defaultName && defaultName !== provider?.name
                ? t("{name} is the default.", { name: defaultName })
                : t("Used when a certificate names no provider.")}
          </p>
        </div>
        <Switch
          aria-label={t("Default provider")}
          checked={f.default}
          onCheckedChange={(v) => set({ default: v })}
        />
      </div>
      {!editing && (
        <label className="flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            className="mt-1"
            checked={f.terms_agreed}
            onChange={(e) => set({ terms_agreed: e.target.checked })}
          />
          <span>
            {t("The controller accepts the CA's terms of service on this account's behalf.")}
            {errors.terms_agreed && (
              <span role="alert" className="block text-xs text-status-down">
                {errors.terms_agreed}
              </span>
            )}
          </span>
        </label>
      )}
      {editing && (
        <div className="space-y-2">
          <Button variant="outline" disabled={runTest.isPending} onClick={() => runTest.mutate()}>
            <RefreshCw className={cn("size-4", runTest.isPending && "animate-spin")} aria-hidden />{" "}
            {t("Test connection")}
          </Button>
          <span className="ml-2 text-xs text-muted-foreground">
            {t("Saved settings only. Save first to test a change.")}
          </span>
          {test && (
            <div
              role="status"
              className="space-y-1.5 rounded-lg border p-3 text-sm"
              aria-label={t("Test result")}
            >
              <div className="font-medium">
                {test.r.ok ? t("Test passed") : t("Test failed")} · {test.at.toLocaleTimeString()}
              </div>
              {test.r.stages.map((s) => (
                <div key={s.name} className="flex items-start gap-2">
                  {s.ok ? (
                    <CheckCircle2
                      className="mt-0.5 size-4 shrink-0 text-status-healthy"
                      aria-hidden
                    />
                  ) : (
                    <XCircle className="mt-0.5 size-4 shrink-0 text-status-down" aria-hidden />
                  )}
                  <span>{s.detail}</span>
                </div>
              ))}
              {!test.r.ok && <p className="text-xs">{test.r.detail}</p>}
              {test.r.tos_url && (
                <p className="text-xs text-muted-foreground">
                  {t("The CA's terms of service:")}{" "}
                  <a
                    href={test.r.tos_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-primary hover:underline"
                  >
                    {test.r.tos_url}
                  </a>
                </p>
              )}
              <p className="text-xs text-muted-foreground">
                {t("Nothing is ordered during a test.")}
              </p>
            </div>
          )}
        </div>
      )}
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete {name}?", { name: provider?.name ?? "" })}
        confirmLabel={t("Delete provider")}
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
      >
        <p>
          {t(
            "The provider and its stored keys are removed. This acts immediately, outside the change set, and is audited.",
          )}
        </p>
      </ConfirmDialog>
    </Sheet>
  );
}

export function AcmeProviders() {
  const list = useProviders();
  const presets = usePresets();
  const [sheet, setSheet] = useState<Provider | "new" | null>(null);
  const items = list.data ?? [];
  const label = (k: string) => presets.data?.find((p) => p.key === k)?.label ?? k;
  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("ACME providers")}</h1>
          <p className="text-sm text-muted-foreground">
            {t(
              "Certificate authorities every tenant can request certificates from. Account keys and secrets are stored in OpenBao.",
            )}
          </p>
        </div>
        <Button disabled={!presets.data} onClick={() => setSheet("new")}>
          {t("Add provider")}
        </Button>
      </div>
      <div className="overflow-x-auto rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={6} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : items.length === 0 ? (
          <EmptyState
            title={t("No ACME providers yet")}
            body={t("Add Let's Encrypt, ZeroSSL, a private CA or any ACME directory.")}
            action={<Button onClick={() => setSheet("new")}>{t("Add provider")}</Button>}
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["Name", "Type", "Directory URL", "Account", "Default", "Used by"].map((h) => (
                  <th key={h} className="px-4 py-2.5 font-medium whitespace-nowrap">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {items.map((p) => (
                <tr key={p.id}>
                  <td className="px-4 py-3">
                    <button
                      type="button"
                      className="font-medium text-primary hover:underline"
                      onClick={() => setSheet(p)}
                    >
                      {p.name}
                    </button>
                  </td>
                  <td className="px-4 py-3">{label(p.preset)}</td>
                  <td className="px-4 py-3 font-mono text-xs break-all">{p.directory_url}</td>
                  <td className="px-4 py-3">
                    <AccountPill p={p} />
                  </td>
                  <td className="px-4 py-3">
                    {p.default ? <StatusPill status="healthy">{t("Default")}</StatusPill> : "—"}
                  </td>
                  <td className="px-4 py-3">{p.used_by === 0 ? t("none") : p.used_by}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {t(
          "Changes here apply at once (no change set) and are audited. A tenant's Request certificate sheet lists the enabled providers; a certificate renews through the one it names.",
        )}
      </p>
      {sheet !== null && presets.data && (
        <ProviderSheet
          key={sheet === "new" ? "new" : sheet.id}
          provider={sheet === "new" ? null : sheet}
          presets={presets.data}
          defaultName={items.find((p) => p.default)?.name}
          onClose={() => setSheet(null)}
        />
      )}
    </div>
  );
}
