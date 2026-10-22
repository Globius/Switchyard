// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { emptyMatch, ExtraMatch } from "@/features/policies/MatchEditor";
import { Combobox } from "@/components/ui/combobox";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { DropdownMenu } from "radix-ui";
import { ApiError, api, unwrap } from "@/api/client";
import { useCan } from "@/auth/session";
import { ChipInput } from "@/components/sy/ChipInput";
import { FindingsSummary, focusField } from "@/components/sy/Findings";
import { expiryStatus } from "@/components/sy/StatusPill";
import { ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Menu, MenuContent, MenuTrigger } from "@/components/ui/popover-menu";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useEnsureDraft } from "@/features/shell/draft-write";
import { useDraft } from "@/features/shell/draft";
import { chipsToLabels, labelChips, validateLabelChip } from "@/lib/labels";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { PoolSheet } from "@/features/pools/Pools";
import { ImportSheet } from "@/features/certificates/Certificates";
import { RequestCertificate } from "@/features/certificates/RequestCertificate";
import { usePools, useVs } from "./detail-queries";
import { useCertificates, useSeGroups } from "./queries";
import {
  buildPatch,
  errorsByField,
  messagesFor,
  toEditable,
  type Editable,
  type VsRead,
} from "./settings-model";

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";

function useProtocolRules(protocol: string | undefined) {
  return useQuery({
    queryKey: ["schema", "virtual-service", protocol],
    enabled: !!protocol,
    staleTime: Infinity,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/schema/virtual-service", {
          params: { query: { protocol: protocol as "http" | "tcp" | "udp" } },
        }),
      ),
  });
}

function usePolicySets(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "policy-sets"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/policy-sets", {
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

function Field({
  label,
  name,
  changed,
  live,
  onRevert,
  errors,
  children,
}: {
  label: string;
  name: string;
  changed: boolean;
  live?: string;
  onRevert?: () => void;
  errors: string[];
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5" data-field={name}>
      <label className="flex items-center gap-2 text-sm font-medium">
        {changed && (
          <span className="size-2 rounded-full bg-draft" aria-label={t("Changed in draft")} />
        )}
        {label}
      </label>
      {children}
      {changed && live !== undefined && (
        <p className="text-xs text-muted-foreground">
          {t("Live: {v}", { v: live })}{" "}
          {onRevert && (
            <button type="button" className="text-primary hover:underline" onClick={onRevert}>
              {t("Revert")}
            </button>
          )}
        </p>
      )}
      {errors.map((m) => (
        <p key={m} role="alert" className="text-xs text-status-down">
          {m}
        </p>
      ))}
    </div>
  );
}

function SettingsCard({
  title,
  subtitle,
  dot,
  action,
  children,
}: {
  title: string;
  subtitle?: string;
  dot?: boolean;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <header className="flex items-center gap-3 border-b px-4 py-3">
        {dot && (
          <span className="size-2 rounded-full bg-draft" aria-label={t("Changed in draft")} />
        )}
        <h2 className="text-base font-semibold">{title}</h2>
        {subtitle && <span className="text-xs text-muted-foreground">{subtitle}</span>}
        {action && <span className="ml-auto">{action}</span>}
      </header>
      <div>{children}</div>
    </section>
  );
}

type PortRow = Editable["ports"][number];

/** The detailed port editor: add, remove and set each port's flags. */
function PortsSheet({
  ports,
  disabled,
  portForbidden,
  errors,
  clientCas,
  onChange,
  onClose,
}: {
  clientCas: { id: string; name: string }[];
  ports: PortRow[];
  disabled: boolean;
  portForbidden: Set<string>;
  errors: Record<string, string[]>;
  onChange: (p: PortRow[]) => void;
  onClose: () => void;
}) {
  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={t("Edit ports")}
      footer={
        <>
          <Button
            type="button"
            variant="outline"
            disabled={disabled}
            onClick={() =>
              onChange([
                ...ports,
                {
                  port: 80,
                  tls: false,
                  http2: false,
                  proxy_protocol: false,
                  https_redirect_port: null,
                  client_verify: "required",
                  tls_passthrough: false,
                  http3: false,
                },
              ])
            }
          >
            {t("Add port")}
          </Button>
          <Button type="button" className="ml-auto" onClick={onClose}>
            {t("Done")}
          </Button>
        </>
      }
    >
      {ports.map((p, i) => (
        <div
          key={i}
          className="flex flex-wrap items-center gap-4 rounded-lg border bg-card p-3 text-sm"
        >
          <input
            aria-label={t("Port {n}", { n: i + 1 })}
            type="number"
            className={cn(INPUT, "w-28 font-mono")}
            disabled={disabled}
            value={p.port}
            onChange={(e) =>
              onChange(ports.map((x, j) => (j === i ? { ...x, port: Number(e.target.value) } : x)))
            }
          />
          {(["tls", "http2", "http3", "proxy_protocol", "tls_passthrough"] as const).map((k) => {
            const name =
              k === "tls"
                ? "TLS"
                : k === "http2"
                  ? "HTTP/2"
                  : k === "http3"
                    ? "HTTP/3"
                    : k === "tls_passthrough"
                      ? t("TLS passthrough")
                      : "PROXY";
            const rule =
              k === "http3"
                ? t("Needs TLS and HTTP/2")
                : k === "tls_passthrough"
                  ? t("Routes by SNI without terminating TLS")
                  : undefined;
            return (
              <label key={k} className="flex items-center gap-1.5" title={rule}>
                <Switch
                  aria-label={name}
                  disabled={
                    disabled ||
                    portForbidden.has(k) ||
                    (k === "http3" && !(p.tls && p.http2)) ||
                    (k !== "tls_passthrough" && k !== "proxy_protocol" && p.tls_passthrough)
                  }
                  checked={p[k] ?? false}
                  onCheckedChange={(v) =>
                    onChange(ports.map((x, j) => (j === i ? { ...x, [k]: v } : x)))
                  }
                />
                {name}
              </label>
            );
          })}
          {p.tls && (
            <label className="flex items-center gap-1.5">
              {t("TLS profile")}
              <select
                aria-label={t("Port {n} TLS profile", { n: i + 1 })}
                className={cn(INPUT, "w-36")}
                disabled={disabled}
                value={p.tls_profile ?? ""}
                onChange={(e) =>
                  onChange(
                    ports.map((x, j) =>
                      j === i
                        ? { ...x, tls_profile: (e.target.value || null) as PortRow["tls_profile"] }
                        : x,
                    ),
                  )
                }
              >
                <option value="">{t("Default")}</option>
                <option value="modern">{t("Modern")}</option>
                <option value="intermediate">{t("Intermediate")}</option>
                <option value="legacy">{t("Legacy")}</option>
              </select>
            </label>
          )}
          {p.tls && (
            <>
              <label className="flex items-center gap-1.5">
                {t("Client CA")}
                <Combobox
                  aria-label={t("Port {n} client CA", { n: i + 1 })}
                  className="w-44"
                  disabled={disabled}
                  value={p.client_ca_id ?? ""}
                  onChange={(v) =>
                    onChange(ports.map((x, j) => (j === i ? { ...x, client_ca_id: v || null } : x)))
                  }
                  options={[
                    { value: "", label: t("None") },
                    ...clientCas.map((c) => ({ value: c.id, label: c.name })),
                  ]}
                />
              </label>
              {p.client_ca_id && (
                <label className="flex items-center gap-1.5">
                  {t("Client certificate")}
                  <select
                    aria-label={t("Port {n} client verification", { n: i + 1 })}
                    className={cn(INPUT, "w-32")}
                    disabled={disabled}
                    value={p.client_verify ?? "required"}
                    onChange={(e) =>
                      onChange(
                        ports.map((x, j) =>
                          j === i
                            ? { ...x, client_verify: e.target.value as PortRow["client_verify"] }
                            : x,
                        ),
                      )
                    }
                  >
                    <option value="required">{t("Required")}</option>
                    <option value="optional">{t("Optional")}</option>
                  </select>
                </label>
              )}
            </>
          )}
          <label className="flex items-center gap-1.5">
            {t("HTTPS redirect to")}
            <input
              aria-label={t("Port {n} redirect to", { n: i + 1 })}
              type="number"
              className={cn(INPUT, "w-24 font-mono")}
              disabled={disabled || p.tls}
              value={p.https_redirect_port ?? ""}
              onChange={(e) =>
                onChange(
                  ports.map((x, j) =>
                    j === i
                      ? {
                          ...x,
                          https_redirect_port: e.target.value ? Number(e.target.value) : null,
                        }
                      : x,
                  ),
                )
              }
            />
          </label>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={disabled || ports.length === 1}
            onClick={() => onChange(ports.filter((_, j) => j !== i))}
          >
            {t("Remove")}
          </Button>
          {messagesFor(errors, `ports.${i}`).map((m) => (
            <span key={m} role="alert" className="w-full text-xs text-status-down">
              {m}
            </span>
          ))}
        </div>
      ))}
    </Sheet>
  );
}

/** Edit every field of a virtual service; Save adds an update op to your draft. */
export function VsSettings({
  tenant,
  id,
  focus,
  addRoute,
}: {
  tenant: string;
  id: string;
  focus?: string;
  addRoute?: boolean;
}) {
  const { draft } = useDraft(tenant);
  const vs = useVs(tenant, id, draft?.id);
  const live = useVs(tenant, id);
  const [generation, setGeneration] = useState(0);
  if (vs.isPending) return <TableSkeleton rows={6} cols={2} />;
  if (vs.isError) return <ErrorState error={vs.error} onRetry={() => void vs.refetch()} />;
  return (
    <SettingsForm
      key={`${vs.data.id}-${generation}`}
      tenant={tenant}
      id={id}
      v={vs.data}
      liveVs={live.data}
      draftState={draft?.state}
      draftId={draft?.id}
      onReload={() => setGeneration((g) => g + 1)}
      focus={focus}
      addRoute={addRoute}
    />
  );
}

function SettingsForm({
  tenant,
  id,
  v,
  liveVs,
  draftState,
  draftId,
  onReload,
  focus,
  addRoute,
}: {
  tenant: string;
  id: string;
  v: VsRead;
  liveVs: VsRead | undefined;
  draftState: string | undefined;
  draftId: string | undefined;
  onReload: () => void;
  focus?: string;
  addRoute?: boolean;
}) {
  const pools = usePools(tenant, draftId);
  const certs = useCertificates(tenant, draftId);
  const groups = useSeGroups(tenant);
  const policies = usePolicySets(tenant);
  const vipPools = useVipPools(tenant);
  const rules = useProtocolRules(v.protocol);
  const can = useCan(tenant);
  const write = can("write", "application");
  const ensure = useEnsureDraft(tenant);
  const qc = useQueryClient();

  const [form, setForm] = useState<Editable>(() => toEditable(v));
  const [base, setBase] = useState<Editable>(() => toEditable(v));
  const [errors, setErrors] = useState<Record<string, string[]>>({});
  const [stale, setStale] = useState(false);
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);
  const [portsOpen, setPortsOpen] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  /** Index of a route to scroll to once it has rendered. */
  const revealRoute = useRef<number | null>(null);
  /** Where a pool created in the dialog goes. `null` when the dialog is closed. */
  const [newPool, setNewPool] = useState<((poolId: string) => void) | null>(null);
  const [certDialog, setCertDialog] = useState<"import" | "issue" | null>(null);
  /** Name of a certificate being issued; it is selected once it shows up in the list. */
  const [awaitedCert, setAwaitedCert] = useState<string | null>(null);

  const appendRoute = () => {
    const at = form.routes.length;
    setForm((f) => ({
      ...f,
      routes: [
        ...f.routes,
        {
          order: f.routes.length + 1,
          match: emptyMatch(),
          pool_id: null,
          pools: [],
          fallback_pool_ids: [],
          redirect: null,
        },
      ],
    }));
    revealRoute.current = at;
  };

  const addedFromLink = useRef(false);
  useEffect(() => {
    if (addRoute && !addedFromLink.current) {
      addedFromLink.current = true;
      appendRoute();
    } else if (focus) focusField(focus);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus, addRoute]);

  const routeCount = form.routes.length;
  useEffect(() => {
    if (revealRoute.current === null || routeCount <= revealRoute.current) return;
    focusField(`routes.${revealRoute.current}`);
    revealRoute.current = null;
  }, [routeCount]);

  useEffect(() => {
    if (!awaitedCert) return;
    const hit = certs.data?.items.find((c) => c.name === awaitedCert);
    if (hit) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- adopting an externally created certificate
      setForm((f) =>
        f.certificate_ids.includes(hit.id)
          ? f
          : { ...f, certificate_ids: [...f.certificate_ids, hit.id] },
      );
      setAwaitedCert(null);
      return;
    }
    // An issued certificate appears when its job has finished: keep looking.
    const timer = setInterval(() => void qc.invalidateQueries({ queryKey: ["t", tenant] }), 4000);
    return () => clearInterval(timer);
  }, [awaitedCert, certs.data, qc, tenant]);

  const patch = useMemo(() => buildPatch(base, form), [form, base]);
  const dirty = Object.keys(patch).length > 0;
  const liveEditable = liveVs ? toEditable(liveVs) : null;

  const forbidden = new Set([...(rules.data?.service_forbidden ?? [])]);
  const portForbidden = new Set(rules.data?.port_forbidden ?? []);
  const set = <K extends keyof Editable>(k: K, val: Editable[K]) =>
    setForm((f) => ({ ...f, [k]: val }));
  const changed = (k: keyof Editable) => v.changed_fields.includes(k);
  const liveText = (k: keyof Editable) =>
    liveEditable ? JSON.stringify(liveEditable[k]) : undefined;
  const revert = (k: keyof Editable) => () => liveEditable && set(k, liveEditable[k]);
  const er = (path: string) => messagesFor(errors, path);
  /** Errors on `field.N` belong to the chip at index N. */
  const itemErrors = (field: string, values: string[]) =>
    Object.fromEntries(
      Object.entries(errors).flatMap(([path, msgs]) => {
        const idx = path.startsWith(`${field}.`) ? Number(path.slice(field.length + 1)) : NaN;
        const val = Number.isInteger(idx) ? values[idx] : undefined;
        return val ? [[val, msgs.join(" ")]] : [];
      }),
    );
  const disabled = !write.allowed;

  /** Ask the pool for its next free address. It is a suggestion: nothing is reserved. */
  async function suggest(poolId: string) {
    try {
      const r = unwrap(
        await api.POST("/api/v1/tenants/{t}/vip-pools/{pool_id}/next-free", {
          params: { path: { t: tenant, pool_id: poolId } },
        }),
      );
      setForm((f) => ({ ...f, vip: r.address, vip_pool_id: poolId }));
      setProblem(null);
    } catch (e) {
      setProblem(e);
    }
  }

  async function save() {
    setSaving(true);
    setErrors({});
    setProblem(null);
    try {
      const cs = await ensure.mutateAsync();
      unwrap(
        await api.PATCH("/api/v1/tenants/{t}/virtual-services/{object_id}", {
          params: {
            path: { t: tenant, object_id: id },
            query: { change_set: cs },
            header: { "if-match": `"${v.version}"` },
          },
          body: patch,
        }),
      );
      setBase(form);
      setSavedAt(
        new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }),
      );
      await qc.invalidateQueries({ queryKey: ["t", tenant] });
    } catch (e) {
      if (e instanceof ApiError && e.status === 412) setStale(true);
      else if (e instanceof ApiError && e.status === 422)
        setErrors(errorsByField(e.problem?.errors));
      else setProblem(e);
    } finally {
      setSaving(false);
    }
  }

  const policyItems = policies.data?.items ?? [];
  const allowLists = policyItems.filter((x) => x.kind === "allow_list");
  const groupLabel = (g: { name: string; ha_mode?: string }) =>
    g.ha_mode ? `${g.name} · ${g.ha_mode.toUpperCase()}` : g.name;
  const poolOptions = pools.data?.items ?? [];
  const vipMode: "fixed" | "pool" = form.vip_pool_id ? "pool" : "fixed";

  return (
    <form
      className="space-y-5"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <p className="text-sm text-muted-foreground">
        {t(
          "Every change here becomes an op in your draft. Nothing goes live until the change set is applied. Changed fields show their live value underneath.",
        )}
      </p>
      <FindingsSummary findings={flatFindings(errors)} />
      {draftState === "pending_approval" && (
        <div
          role="status"
          className="rounded-lg border bg-pending-approval-bg p-3 text-sm text-pending-approval"
        >
          {t("Your draft is awaiting approval. Saving a change withdraws the request.")}
        </div>
      )}
      {stale && (
        <div
          role="alert"
          className="space-y-2 rounded-lg border border-status-degraded/40 bg-status-degraded-bg p-4 text-sm"
        >
          <div className="font-semibold">{t("This service changed while you were editing")}</div>
          <p>{t("Reload to see their change. Your unsaved input stays below until you reload.")}</p>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => {
                void qc.invalidateQueries({ queryKey: ["t", tenant] }).then(onReload);
              }}
            >
              {t("Reload and discard my input")}
            </Button>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void navigator.clipboard?.writeText(JSON.stringify(patch, null, 2))}
            >
              {t("Copy my values")}
            </Button>
          </div>
        </div>
      )}
      {problem != null && <ErrorState error={problem} />}

      <SettingsCard title={t("General")}>
        <div className="grid gap-x-8 gap-y-5 p-4 md:grid-cols-2">
          <Field
            label={t("Name")}
            name="name"
            changed={changed("name")}
            live={liveText("name")}
            onRevert={revert("name")}
            errors={er("name")}
          >
            <input
              aria-label={t("Name")}
              className={INPUT}
              disabled={disabled}
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              {t("Lower case, digits and hyphens. Used in rendered config as {fe}.", {
                fe: `fe_${form.name.replace(/-/g, "_")}`,
              })}
            </p>
          </Field>
          <div className="space-y-1.5">
            <div className="text-sm font-medium">{t("Protocol")}</div>
            <div
              role="radiogroup"
              aria-label={t("Protocol")}
              className="flex overflow-hidden rounded-md border"
            >
              {(["http", "tcp", "udp"] as const).map((k) => (
                <button
                  key={k}
                  type="button"
                  role="radio"
                  aria-checked={v.protocol === k}
                  disabled
                  className={cn(
                    "flex-1 px-4 py-1.5 text-sm",
                    v.protocol === k ? "bg-accent font-medium" : "text-muted-foreground",
                  )}
                >
                  {k.toUpperCase()}
                </button>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">
              {t("The protocol cannot be changed here; create a new service for another one.")}
            </p>
          </div>
          <Field
            label={t("SE group")}
            name="se_group_id"
            changed={changed("se_group_id")}
            live={liveText("se_group_id")}
            onRevert={revert("se_group_id")}
            errors={er("se_group_id")}
          >
            <Combobox
              aria-label={t("SE group")}
              disabled={disabled}
              value={form.se_group_id}
              onChange={(v) => set("se_group_id", v)}
              options={(groups.data?.items ?? []).map((g) => ({
                value: g.id,
                label: groupLabel(g),
              }))}
            />
          </Field>
          <Field
            label={t("Enabled")}
            name="enabled"
            changed={changed("enabled")}
            live={liveText("enabled")}
            onRevert={revert("enabled")}
            errors={er("enabled")}
          >
            <div className="flex h-9 items-center gap-3">
              <Switch
                aria-label={t("Enabled")}
                disabled={disabled}
                checked={form.enabled}
                onCheckedChange={(on) => set("enabled", on)}
              />
              <span className="text-sm text-muted-foreground">
                {t("Disabled services are not rendered")}
              </span>
            </div>
          </Field>
          <Field
            label={t("Labels")}
            name="labels"
            changed={changed("labels")}
            live={liveText("labels")}
            onRevert={revert("labels")}
            errors={er("labels")}
          >
            <ChipInput
              label={t("Labels")}
              value={labelChips(form.labels)}
              onChange={(chips) => set("labels", chipsToLabels(chips))}
              freeText={{ validate: (x) => validateLabelChip(x) && t(validateLabelChip(x)!) }}
              placeholder="env=prod"
              disabled={disabled}
            />
          </Field>
        </div>
      </SettingsCard>

      <SettingsCard title={t("Listener")}>
        <div className="grid gap-x-8 gap-y-5 p-4 md:grid-cols-2">
          <Field
            label={t("VIP")}
            name="vip"
            changed={changed("vip") || changed("vip_pool_id")}
            live={changed("vip") ? liveText("vip") : undefined}
            onRevert={() => {
              revert("vip")();
              revert("vip_pool_id")();
            }}
            errors={er("vip")}
          >
            <div role="radiogroup" aria-label={t("VIP source")} className="space-y-2">
              <div
                className={cn(
                  "flex gap-3 rounded-lg border p-3",
                  vipMode === "fixed" && "border-primary bg-primary/10",
                )}
              >
                <input
                  type="radio"
                  name="vip-mode"
                  aria-label={t("Fixed address")}
                  disabled={disabled}
                  checked={vipMode === "fixed"}
                  onChange={() => set("vip_pool_id", null)}
                />
                <div className="flex-1">
                  <div className="font-medium">{t("Fixed address")}</div>
                  <input
                    aria-label={t("VIP")}
                    data-focus
                    className={cn(INPUT, "mt-1.5 font-mono")}
                    disabled={disabled || vipMode !== "fixed"}
                    value={form.vip ?? ""}
                    placeholder={t("allocated from the VIP pool")}
                    onChange={(e) => set("vip", e.target.value)}
                  />
                  <div className="mt-1 text-xs text-muted-foreground">
                    {t("IPv4 or IPv6, inside a network of the SE group")}
                  </div>
                </div>
              </div>
              <div
                className={cn(
                  "flex gap-3 rounded-lg border p-3",
                  vipMode === "pool" && "border-primary bg-primary/10",
                )}
              >
                <input
                  type="radio"
                  name="vip-mode"
                  aria-label={t("Suggest from a VIP pool")}
                  disabled={disabled}
                  checked={vipMode === "pool"}
                  onChange={() => set("vip_pool_id", vipPools.data?.items[0]?.id ?? null)}
                />
                <div className="flex-1">
                  <div className="font-medium">{t("Suggest from a VIP pool")}</div>
                  <Combobox
                    aria-label={t("VIP pool")}
                    className="mt-1.5"
                    disabled={disabled || vipMode !== "pool"}
                    value={form.vip_pool_id ?? ""}
                    onChange={(v) => set("vip_pool_id", v || null)}
                    options={(vipPools.data?.items ?? []).map((p) => ({
                      value: p.id,
                      label: p.name,
                    }))}
                  />
                  <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                    {t("fills in the lowest free address now; it isn't reserved")}
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={disabled || vipMode !== "pool" || !form.vip_pool_id}
                      onClick={() => void suggest(form.vip_pool_id!)}
                    >
                      {t("Suggest")}
                    </Button>
                  </div>
                </div>
              </div>
            </div>
          </Field>
          <div className="space-y-1.5" data-field="ports">
            <div className="text-sm font-medium">{t("Ports")}</div>
            {(errors.ports ?? []).map((m) => (
              <p key={m} role="alert" className="text-xs text-status-down">
                {m}
              </p>
            ))}
            <table className="w-full overflow-hidden rounded-md border text-[13px]">
              <thead className="text-left text-xs text-muted-foreground">
                <tr>
                  {["Port", "TLS", "HTTP/2", "PROXY", "Redirect"].map((h) => (
                    <th key={h} className="px-3 py-2 font-medium">
                      {t(h)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y">
                {form.ports.map((p, i) => (
                  <tr key={i} data-field={`ports.${i}`}>
                    <td className="px-3 py-2 font-mono">{p.port}</td>
                    <td className="px-3 py-2">
                      {p.tls
                        ? p.tls_profile
                          ? `${t("On")} · ${p.tls_profile}`
                          : t("On")
                        : t("Off")}
                    </td>
                    <td className="px-3 py-2">{p.http2 ? t("On") : t("Off")}</td>
                    <td className="px-3 py-2">{p.proxy_protocol ? t("On") : t("Off")}</td>
                    <td className="px-3 py-2">{p.https_redirect_port ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {form.ports.flatMap((_, i) =>
              messagesFor(errors, `ports.${i}`).map((m) => (
                <p key={`${i}-${m}`} role="alert" className="text-xs text-status-down">
                  {m}
                </p>
              )),
            )}
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={disabled}
              onClick={() => setPortsOpen(true)}
            >
              {t("Edit ports")}
            </Button>
          </div>
          {!forbidden.has("certificate_ids") && (
            <Field
              label={t("Certificates")}
              name="certificate_ids"
              changed={changed("certificate_ids")}
              live={liveText("certificate_ids")}
              onRevert={revert("certificate_ids")}
              errors={errors.certificate_ids ?? []}
            >
              <ChipInput
                label={t("Certificates")}
                disabled={disabled}
                value={form.certificate_ids}
                onChange={(val) => set("certificate_ids", val)}
                options={(certs.data?.items ?? []).map((c) => ({
                  value: c.id,
                  label: c.name,
                  hint: expiryStatus(c.not_after).label,
                }))}
                itemErrors={itemErrors("certificate_ids", form.certificate_ids)}
                emptyMessage={(q) => t("No certificate named “{q}”.", { q })}
                placeholder={t("Add certificate")}
              />
              <div className="flex gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={disabled}
                  onClick={() => setCertDialog("import")}
                >
                  {t("Import certificate…")}
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  disabled={disabled}
                  onClick={() => setCertDialog("issue")}
                >
                  {t("Request certificate…")}
                </Button>
              </div>
            </Field>
          )}
          <Field
            label={t("Restore client IP header")}
            name="restore_client_ip_header"
            changed={changed("restore_client_ip_header")}
            live={liveText("restore_client_ip_header")}
            onRevert={revert("restore_client_ip_header")}
            errors={er("restore_client_ip_header")}
          >
            <select
              aria-label={t("Restore client IP header")}
              className={INPUT}
              disabled={disabled}
              value={form.restore_client_ip_header ?? ""}
              onChange={(e) => set("restore_client_ip_header", e.target.value || null)}
            >
              <option value="">{t("None")}</option>
              {["X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP"].map((h) => (
                <option key={h} value={h}>
                  {h}
                </option>
              ))}
            </select>
          </Field>
        </div>
      </SettingsCard>

      {!forbidden.has("routes") && (
        <SettingsCard
          title={t("Routes")}
          subtitle={t("first match wins · reorder from the row menu")}
          dot={changed("routes")}
          action={
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={disabled}
              onClick={appendRoute}
            >
              {t("Add route")}
            </Button>
          }
        >
          {(errors.routes ?? []).map((m) => (
            <p key={m} role="alert" className="px-4 pt-3 text-xs text-status-down">
              {m}
            </p>
          ))}
          {form.routes.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-[13px]">
                <thead className="border-b text-left text-xs text-muted-foreground">
                  <tr>
                    {["#", "Host", "Path match", "Path", "Action", "Target", ""].map((h, i) => (
                      <th key={i} className="px-3 py-2 font-medium">
                        {t(h)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {form.routes.map((r, i) => {
                    const upd = (patchR: Partial<typeof r>) =>
                      set(
                        "routes",
                        form.routes.map((x, j) => (j === i ? { ...x, ...patchR } : x)),
                      );
                    const redirect = r.redirect != null;
                    const split = (r.pools ?? []).length > 0;
                    const exact = r.match.path_exact != null;
                    const msgs = messagesFor(errors, `routes.${i}`);
                    return (
                      <Fragment key={i}>
                        <tr
                          data-field={`routes.${i}`}
                          className={cn(msgs.length > 0 && "bg-draft/10")}
                        >
                          <td className="px-3 py-2 text-muted-foreground">{i + 1}</td>
                          <td className="px-3 py-2">
                            <input
                              aria-label={t("Route {n} host", { n: i + 1 })}
                              placeholder={t("host")}
                              className={INPUT}
                              disabled={disabled}
                              value={r.match.host ?? ""}
                              onChange={(e) =>
                                upd({ match: { ...r.match, host: e.target.value || null } })
                              }
                            />
                          </td>
                          <td className="px-3 py-2">
                            <select
                              aria-label={t("Route {n} path match", { n: i + 1 })}
                              className={cn(INPUT, "w-28")}
                              disabled={disabled}
                              value={exact ? "exact" : "prefix"}
                              onChange={(e) => {
                                const val = r.match.path_exact ?? r.match.path_prefix ?? null;
                                upd({
                                  match: {
                                    ...r.match,
                                    path_exact: e.target.value === "exact" ? val : null,
                                    path_prefix: e.target.value === "exact" ? null : val,
                                  },
                                });
                              }}
                            >
                              <option value="prefix">{t("Prefix")}</option>
                              <option value="exact">{t("Exact")}</option>
                            </select>
                          </td>
                          <td className="px-3 py-2">
                            <input
                              aria-label={t("Route {n} path prefix", { n: i + 1 })}
                              placeholder={t("path")}
                              className={cn(INPUT, "font-mono")}
                              disabled={disabled}
                              value={(exact ? r.match.path_exact : r.match.path_prefix) ?? ""}
                              onChange={(e) =>
                                upd({
                                  match: exact
                                    ? { ...r.match, path_exact: e.target.value || null }
                                    : { ...r.match, path_prefix: e.target.value || null },
                                })
                              }
                            />
                          </td>
                          <td className="px-3 py-2">
                            <select
                              aria-label={t("Route {n} action", { n: i + 1 })}
                              className={cn(INPUT, "w-28")}
                              disabled={disabled}
                              value={redirect ? "redirect" : split ? "split" : "pool"}
                              onChange={(e) =>
                                upd(
                                  e.target.value === "redirect"
                                    ? {
                                        pool_id: null,
                                        pools: [],
                                        redirect: { location: "/", code: 301 },
                                      }
                                    : e.target.value === "split"
                                      ? {
                                          redirect: null,
                                          pool_id: null,
                                          pools: r.pool_id
                                            ? [
                                                { pool_id: r.pool_id, weight: 90 },
                                                { pool_id: "", weight: 10 },
                                              ]
                                            : [
                                                { pool_id: "", weight: 50 },
                                                { pool_id: "", weight: 50 },
                                              ],
                                        }
                                      : { redirect: null, pools: [] },
                                )
                              }
                            >
                              <option value="pool">{t("Pool")}</option>
                              <option value="split">{t("Split")}</option>
                              <option value="redirect">{t("Redirect")}</option>
                            </select>
                          </td>
                          <td className="px-3 py-2">
                            {redirect ? (
                              <input
                                aria-label={t("Route {n} redirect location", { n: i + 1 })}
                                className={cn(INPUT, "font-mono")}
                                disabled={disabled}
                                value={r.redirect?.location ?? ""}
                                onChange={(e) =>
                                  upd({
                                    redirect: {
                                      location: e.target.value,
                                      code: r.redirect?.code ?? 301,
                                    },
                                  })
                                }
                              />
                            ) : split ? (
                              <div className="space-y-1.5">
                                {(r.pools ?? []).map((wp, k) => (
                                  <div key={k} className="flex items-center gap-2">
                                    <div className="min-w-0 flex-1">
                                      <Combobox
                                        aria-label={t("Route {n} split pool {k}", {
                                          n: i + 1,
                                          k: k + 1,
                                        })}
                                        placeholder={t("Pool")}
                                        disabled={disabled}
                                        value={wp.pool_id}
                                        onChange={(v) =>
                                          upd({
                                            pools: (r.pools ?? []).map((x, j) =>
                                              j === k ? { ...x, pool_id: v } : x,
                                            ),
                                          })
                                        }
                                        options={poolOptions.map((p) => ({
                                          value: p.id,
                                          label: p.name,
                                        }))}
                                      />
                                      <NewPoolButton
                                        label={t("New pool for route {n} split pool {k}", {
                                          n: i + 1,
                                          k: k + 1,
                                        })}
                                        disabled={disabled}
                                        onClick={() =>
                                          setNewPool(
                                            () => (pid: string) =>
                                              upd({
                                                pools: (r.pools ?? []).map((x, j) =>
                                                  j === k ? { ...x, pool_id: pid } : x,
                                                ),
                                              }),
                                          )
                                        }
                                      />
                                    </div>
                                    <input
                                      aria-label={t("Route {n} split weight {k}", {
                                        n: i + 1,
                                        k: k + 1,
                                      })}
                                      type="number"
                                      min={1}
                                      max={1000}
                                      className={cn(INPUT, "w-20")}
                                      disabled={disabled}
                                      value={wp.weight}
                                      onChange={(e) =>
                                        upd({
                                          pools: (r.pools ?? []).map((x, j) =>
                                            j === k ? { ...x, weight: Number(e.target.value) } : x,
                                          ),
                                        })
                                      }
                                    />
                                    <span className="w-10 text-xs text-muted-foreground">
                                      {Math.round(
                                        (wp.weight /
                                          Math.max(
                                            1,
                                            (r.pools ?? []).reduce((a, x) => a + x.weight, 0),
                                          )) *
                                          100,
                                      )}
                                      %
                                    </span>
                                    <Button
                                      type="button"
                                      size="sm"
                                      variant="ghost"
                                      disabled={disabled || (r.pools ?? []).length <= 2}
                                      aria-label={t("Remove split pool {k}", { k: k + 1 })}
                                      onClick={() =>
                                        upd({ pools: (r.pools ?? []).filter((_, j) => j !== k) })
                                      }
                                    >
                                      ×
                                    </Button>
                                  </div>
                                ))}
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="outline"
                                  disabled={disabled}
                                  onClick={() =>
                                    upd({
                                      pools: [...(r.pools ?? []), { pool_id: "", weight: 10 }],
                                    })
                                  }
                                >
                                  {t("Add pool")}
                                </Button>
                                <SplitBar weights={(r.pools ?? []).map((x) => x.weight)} />
                              </div>
                            ) : (
                              <div className="space-y-1.5">
                                <Combobox
                                  aria-label={t("Route {n} pool", { n: i + 1 })}
                                  placeholder={t("Pool")}
                                  disabled={disabled}
                                  value={r.pool_id ?? ""}
                                  onChange={(v) => upd({ pool_id: v || null })}
                                  options={poolOptions.map((p) => ({ value: p.id, label: p.name }))}
                                />
                                <NewPoolButton
                                  label={t("New pool for route {n}", { n: i + 1 })}
                                  disabled={disabled}
                                  onClick={() =>
                                    setNewPool(() => (pid: string) => upd({ pool_id: pid }))
                                  }
                                />
                                {r.pool_id && (
                                  <p className="text-xs font-medium text-muted-foreground">
                                    {t("If it's down, try:")}
                                  </p>
                                )}
                                {r.pool_id && (
                                  <ChipInput
                                    label={t("Route {n} fallback pools", { n: i + 1 })}
                                    value={r.fallback_pool_ids ?? []}
                                    onChange={(v) => upd({ fallback_pool_ids: v.slice(0, 3) })}
                                    options={poolOptions
                                      .filter((p) => p.id !== r.pool_id)
                                      .map((p) => ({ value: p.id, label: p.name }))}
                                    placeholder={t("If it is down, try…")}
                                    disabled={disabled}
                                  />
                                )}
                              </div>
                            )}
                          </td>
                          <td className="px-3 py-2 text-right">
                            <Menu>
                              <MenuTrigger asChild>
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="ghost"
                                  aria-label={t("Route {n} actions", { n: i + 1 })}
                                  disabled={disabled}
                                >
                                  ⋮
                                </Button>
                              </MenuTrigger>
                              <MenuContent className="w-44">
                                {[
                                  {
                                    label: t("Move up"),
                                    off: i === 0,
                                    run: () => set("routes", swap(form.routes, i, i - 1)),
                                  },
                                  {
                                    label: t("Move down"),
                                    off: i === form.routes.length - 1,
                                    run: () => set("routes", swap(form.routes, i, i + 1)),
                                  },
                                  {
                                    label: t("Remove"),
                                    off: false,
                                    run: () =>
                                      set(
                                        "routes",
                                        renumber(form.routes.filter((_, j) => j !== i)),
                                      ),
                                  },
                                ].map((it) => (
                                  <DropdownMenu.Item
                                    key={it.label}
                                    disabled={it.off}
                                    onSelect={it.run}
                                    className="cursor-default rounded-md px-3 py-2 text-sm outline-none data-[disabled]:opacity-50 data-[highlighted]:bg-accent"
                                  >
                                    {it.label}
                                  </DropdownMenu.Item>
                                ))}
                              </MenuContent>
                            </Menu>
                          </td>
                        </tr>
                        <tr>
                          <td />
                          <td colSpan={6} className="px-3 pb-2">
                            <ExtraMatch
                              id={`Route ${i + 1}`}
                              match={r.match}
                              disabled={disabled}
                              onChange={(m) => upd({ match: m })}
                            />
                          </td>
                        </tr>
                        {msgs.length > 0 && (
                          <tr className="bg-draft/10">
                            <td />
                            <td colSpan={6} className="px-3 pb-2 text-xs text-status-degraded">
                              {msgs.map((m) => (
                                <span key={m} role="alert" className="mr-3">
                                  {m}
                                </span>
                              ))}
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
          <div className="border-t p-4">
            <Field
              label={t("Default pool")}
              name="default_pool_id"
              changed={changed("default_pool_id")}
              live={liveText("default_pool_id")}
              onRevert={revert("default_pool_id")}
              errors={er("default_pool_id")}
            >
              <Combobox
                aria-label={t("Default pool")}
                className="max-w-md"
                disabled={disabled}
                value={form.default_pool_id ?? ""}
                onChange={(v) => set("default_pool_id", v || null)}
                options={[
                  { value: "", label: t("None") },
                  ...poolOptions.map((p) => ({ value: p.id, label: p.name })),
                ]}
              />
              <NewPoolButton
                label={t("New pool for default pool")}
                disabled={disabled}
                onClick={() => setNewPool(() => (pid: string) => set("default_pool_id", pid))}
              />
              <p className="text-xs text-muted-foreground">{t("Used when no route matches.")}</p>
            </Field>
          </div>
        </SettingsCard>
      )}

      <SettingsCard title={t("Policies and access")}>
        <div className="grid gap-x-8 gap-y-5 p-4 md:grid-cols-2">
          <Field
            label={t("Policy sets")}
            name="policy_set_ids"
            changed={changed("policy_set_ids")}
            live={liveText("policy_set_ids")}
            onRevert={revert("policy_set_ids")}
            errors={errors.policy_set_ids ?? []}
          >
            <ChipInput
              label={t("Policy sets")}
              disabled={disabled}
              value={form.policy_set_ids}
              onChange={(val) => set("policy_set_ids", val)}
              options={policyItems.map((x) => ({ value: x.id, label: x.name, hint: x.kind }))}
              itemErrors={itemErrors("policy_set_ids", form.policy_set_ids)}
              emptyMessage={(q) => t("No policy named “{q}”.", { q })}
              placeholder={t("Add policy set")}
            />
            <p className="text-xs text-muted-foreground">{t("Applied in order, before routes.")}</p>
          </Field>
          <Field
            label={t("Source allow list")}
            name="allow_list_id"
            changed={changed("allow_list_id")}
            live={liveText("allow_list_id")}
            onRevert={revert("allow_list_id")}
            errors={er("allow_list_id")}
          >
            <Combobox
              aria-label={t("Source allow list")}
              disabled={disabled}
              value={form.allow_list_id ?? ""}
              onChange={(v) => set("allow_list_id", v || null)}
              options={[
                { value: "", label: t("None") },
                ...allowLists.map((x) => ({ value: x.id, label: x.name })),
              ]}
            />
          </Field>
          <Field
            label={t("Deny list")}
            name="deny_list_id"
            changed={changed("deny_list_id")}
            live={liveText("deny_list_id")}
            onRevert={revert("deny_list_id")}
            errors={er("deny_list_id")}
          >
            <Combobox
              aria-label={t("Deny list")}
              disabled={disabled || v.protocol === "udp"}
              value={form.deny_list_id ?? ""}
              onChange={(val) => set("deny_list_id", val || null)}
              options={[
                { value: "", label: t("None") },
                ...allowLists.map((x) => ({ value: x.id, label: x.name })),
              ]}
            />
            <p className="text-xs text-muted-foreground">
              {t(
                "Refuses these networks before anything else (HTTP 403, TCP reset). Use a country or reputation list, for example the CrowdSec bans. UDP services cannot use it.",
              )}
            </p>
          </Field>
          <Field
            label={t("Enforce allow list")}
            name="enforce_allow_list"
            changed={changed("enforce_allow_list")}
            live={liveText("enforce_allow_list")}
            onRevert={revert("enforce_allow_list")}
            errors={er("enforce_allow_list")}
          >
            <div className="flex items-center gap-3">
              <Switch
                aria-label={t("Enforce allow list")}
                disabled={disabled}
                checked={form.enforce_allow_list}
                onCheckedChange={(on) => set("enforce_allow_list", on)}
              />
              <span className="text-sm text-muted-foreground">{t("Off means log only.")}</span>
            </div>
          </Field>
        </div>
      </SettingsCard>

      {v.protocol === "http" || form.cache != null ? (
        <SettingsCard title={t("Response cache")}>
          <div className="space-y-4 p-4" data-field="cache">
            <div className="flex items-start gap-3">
              <Switch
                aria-label={t("Cache responses")}
                disabled={disabled}
                checked={form.cache != null}
                onCheckedChange={(on) =>
                  set("cache", on ? { size_mb: 16, max_object_kb: 1024, max_age_s: 60 } : null)
                }
              />
              <div className="text-sm">
                <div className="font-medium">{t("Cache responses")}</div>
                <p className="text-xs text-muted-foreground">
                  {t(
                    "Stores responses the origin marks cacheable, for GET and HEAD requests without Authorization. Rate limits and deny rules run first. The caches of one SE group share a memory quota.",
                  )}
                </p>
              </div>
            </div>
            {form.cache && (
              <div className="grid gap-3 sm:grid-cols-3">
                {(
                  [
                    ["size_mb", "Memory (MB)", 1, 256],
                    ["max_object_kb", "Largest object (KB)", 1, 8192],
                    ["max_age_s", "Longest age (s)", 1, 86400],
                  ] as const
                ).map(([k, label, min, max]) => (
                  <label key={k} className="space-y-1.5 text-sm font-medium">
                    {t(label)}
                    <input
                      aria-label={t(label)}
                      type="number"
                      min={min}
                      max={max}
                      className={INPUT}
                      disabled={disabled}
                      value={form.cache![k] ?? ""}
                      onChange={(e) =>
                        set("cache", { ...form.cache!, [k]: Number(e.target.value) })
                      }
                    />
                  </label>
                ))}
              </div>
            )}
            {er("cache").map((m) => (
              <p key={m} role="alert" className="text-xs text-status-down">
                {m}
              </p>
            ))}
          </div>
        </SettingsCard>
      ) : null}
      {v.protocol === "http" || form.compression != null ? (
        <SettingsCard title={t("Compression")}>
          <div className="space-y-4 p-4" data-field="compression">
            <div className="flex items-start gap-3">
              <Switch
                aria-label={t("Compress responses")}
                disabled={disabled}
                checked={form.compression != null}
                onCheckedChange={(on) =>
                  set(
                    "compression",
                    on
                      ? {
                          algorithms: ["gzip"],
                          content_types: [
                            "text/html",
                            "text/plain",
                            "text/css",
                            "text/javascript",
                            "application/javascript",
                            "application/json",
                            "application/xml",
                            "image/svg+xml",
                          ],
                        }
                      : null,
                  )
                }
              />
              <div className="text-sm">
                <div className="font-medium">{t("Compress responses")}</div>
                <p className="text-xs text-muted-foreground">
                  {t("For clients that accept it. Never images or archives.")}
                </p>
              </div>
            </div>
            {form.compression && (
              <>
                <div className="flex gap-4 text-sm">
                  {(["gzip", "deflate"] as const).map((a) => (
                    <label key={a} className="flex items-center gap-1.5">
                      <input
                        type="checkbox"
                        aria-label={a}
                        disabled={disabled}
                        checked={form.compression!.algorithms.includes(a)}
                        onChange={(e) =>
                          set("compression", {
                            ...form.compression!,
                            algorithms: e.target.checked
                              ? [...form.compression!.algorithms, a]
                              : form.compression!.algorithms.filter((x) => x !== a),
                          })
                        }
                      />
                      {a}
                    </label>
                  ))}
                </div>
                <ChipInput
                  label={t("Content types")}
                  value={form.compression.content_types}
                  onChange={(v) => set("compression", { ...form.compression!, content_types: v })}
                  freeText={{
                    validate: (x) =>
                      /^[a-z]+\/[a-z0-9.+-]+$/i.test(x.trim())
                        ? null
                        : t("Use a media type such as text/html."),
                  }}
                  placeholder={t("Add a content type")}
                  disabled={disabled}
                />
                {er("compression").map((m) => (
                  <p key={m} role="alert" className="text-xs text-status-down">
                    {m}
                  </p>
                ))}
              </>
            )}
          </div>
        </SettingsCard>
      ) : null}

      <div className="flex items-center gap-3 pb-4">
        <span className="text-xs text-muted-foreground">
          {savedAt && !dirty
            ? t("Saved to draft at {time} · version {n}", { time: savedAt, n: v.version })
            : dirty
              ? t("{n} unsaved fields", { n: Object.keys(patch).length })
              : ""}
        </span>
        <Button
          type="button"
          variant="ghost"
          className="ml-auto"
          disabled={!dirty}
          onClick={() => setForm(base)}
        >
          {t("Revert all fields")}
        </Button>
        <Tip label={write.reason}>
          <Button type="submit" disabled={!write.allowed || !dirty || saving || stale}>
            {t("Save to draft")}
          </Button>
        </Tip>
      </div>

      {portsOpen && (
        <PortsSheet
          ports={form.ports}
          disabled={disabled}
          portForbidden={portForbidden}
          errors={errors}
          clientCas={(certs.data?.items ?? []).filter((c) => c.purpose === "client_ca")}
          onChange={(ports) => set("ports", ports)}
          onClose={() => setPortsOpen(false)}
        />
      )}
      {newPool && (
        <PoolSheet
          tenant={tenant}
          pool="new"
          onClose={() => setNewPool(null)}
          onSaved={(pid) => {
            newPool(pid);
            void qc.invalidateQueries({ queryKey: ["t", tenant, "pools"] });
          }}
        />
      )}
      {certDialog === "import" && (
        <ImportSheet
          tenant={tenant}
          onClose={() => setCertDialog(null)}
          onCreated={(cid) => {
            set("certificate_ids", [...form.certificate_ids.filter((x) => x !== cid), cid]);
            void qc.invalidateQueries({ queryKey: ["t", tenant, "certificates"] });
          }}
        />
      )}
      {certDialog === "issue" && (
        <RequestCertificate
          tenant={tenant}
          onClose={() => setCertDialog(null)}
          onDone={() => void qc.invalidateQueries({ queryKey: ["t", tenant] })}
          onIssued={setAwaitedCert}
        />
      )}
    </form>
  );
}

function swap<T extends { order: number }>(a: T[], i: number, j: number): T[] {
  const out = [...a];
  [out[i], out[j]] = [out[j]!, out[i]!];
  return renumber(out);
}

function renumber<T extends { order: number }>(a: T[]): T[] {
  return a.map((r, i) => ({ ...r, order: i + 1 }) as T);
}

/** Server findings keyed by path, flattened for the summary alert. */
function flatFindings(errors: Record<string, string[]>) {
  return Object.entries(errors).flatMap(([path, msgs]) =>
    msgs.map((message) => ({ path, message })),
  );
}

/** A bar of each pool's share and the total; weights are relative, so 100 reads as percent. */
export function SplitBar({ weights }: { weights: number[] }) {
  const total = weights.reduce((a, x) => a + x, 0);
  return (
    <div className="space-y-1">
      <div className="flex h-2 overflow-hidden rounded bg-muted" aria-hidden>
        {weights.map((w, i) => (
          <div
            key={i}
            className={i % 2 === 0 ? "bg-primary" : "bg-primary/50"}
            style={{ width: `${(w / Math.max(1, total)) * 100}%` }}
          />
        ))}
      </div>
      <p
        role="status"
        className={cn("text-xs", total === 100 ? "text-status-healthy" : "text-muted-foreground")}
      >
        {total === 100
          ? t("Total 100 %")
          : t("Total {n}. Weights are relative; make them add up to 100 to read them as %.", {
              n: total,
            })}
      </p>
    </div>
  );
}

/** Opens the "new pool" dialog from a pool picker. */
function NewPoolButton({
  onClick,
  disabled,
  label,
}: {
  onClick: () => void;
  disabled?: boolean;
  label: string;
}) {
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
    >
      {t("New pool…")}
    </Button>
  );
}
