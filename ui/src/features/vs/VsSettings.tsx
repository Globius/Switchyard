import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import { useCan } from "@/auth/session";
import { ErrorState, TableSkeleton } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useEnsureDraft } from "@/features/shell/draft-write";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
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

/** Edit every field of a virtual service; Save adds an update op to your draft. */
export function VsSettings({ tenant, id }: { tenant: string; id: string }) {
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
      onReload={() => setGeneration((g) => g + 1)}
    />
  );
}

function SettingsForm({
  tenant,
  id,
  v,
  liveVs,
  draftState,
  onReload,
}: {
  tenant: string;
  id: string;
  v: VsRead;
  liveVs: VsRead | undefined;
  draftState: string | undefined;
  onReload: () => void;
}) {
  const pools = usePools(tenant);
  const certs = useCertificates(tenant);
  const groups = useSeGroups(tenant);
  const policies = usePolicySets(tenant);
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

  const patch = useMemo(() => buildPatch(base, form), [form, base]);
  const dirty = Object.keys(patch).length > 0;
  const liveEditable = liveVs ? toEditable(liveVs) : null;

  const forbidden = new Set([...(rules.data?.service_forbidden ?? [])]);
  const portForbidden = new Set(rules.data?.port_forbidden ?? []);
  const set = <K extends keyof Editable>(k: K, val: Editable[K]) => setForm({ ...form, [k]: val });
  const changed = (k: keyof Editable) => v.changed_fields.includes(k);
  const liveText = (k: keyof Editable) =>
    liveEditable ? JSON.stringify(liveEditable[k]) : undefined;
  const revert = (k: keyof Editable) => () => liveEditable && set(k, liveEditable[k]);
  const er = (path: string) => messagesFor(errors, path);
  const disabled = !write.allowed;

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

  return (
    <form
      className="max-w-3xl space-y-8"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
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

      <section className="space-y-4">
        <h2 className="text-lg font-semibold">{t("Basics")}</h2>
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
        </Field>
        <Field
          label={t("Enabled")}
          name="enabled"
          changed={changed("enabled")}
          live={liveText("enabled")}
          onRevert={revert("enabled")}
          errors={er("enabled")}
        >
          <input
            type="checkbox"
            aria-label={t("Enabled")}
            disabled={disabled}
            checked={form.enabled}
            onChange={(e) => set("enabled", e.target.checked)}
          />
        </Field>
        <Field
          label={t("SE group")}
          name="se_group_id"
          changed={changed("se_group_id")}
          live={liveText("se_group_id")}
          onRevert={revert("se_group_id")}
          errors={er("se_group_id")}
        >
          <select
            aria-label={t("SE group")}
            className={INPUT}
            disabled={disabled}
            value={form.se_group_id}
            onChange={(e) => set("se_group_id", e.target.value)}
          >
            {groups.data?.items.map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </Field>
        <p className="text-xs text-muted-foreground">
          {t("Protocol")}: <Badge>{v.protocol.toUpperCase()}</Badge>{" "}
          {t("(cannot be changed; create a new service)")}
        </p>
      </section>

      <section className="space-y-4">
        <h2 className="text-lg font-semibold">{t("Addressing")}</h2>
        <Field
          label={t("VIP")}
          name="vip"
          changed={changed("vip")}
          live={liveText("vip")}
          onRevert={revert("vip")}
          errors={er("vip")}
        >
          <input
            aria-label={t("VIP")}
            className={cn(INPUT, "font-mono")}
            disabled={disabled}
            value={form.vip ?? ""}
            placeholder={t("allocated from the VIP pool")}
            onChange={(e) => set("vip", e.target.value)}
          />
        </Field>
      </section>

      <section className="space-y-3">
        <h2 className="text-lg font-semibold">{t("Ports")}</h2>
        {(errors.ports ?? []).map((m) => (
          <p key={m} role="alert" className="text-xs text-status-down">
            {m}
          </p>
        ))}
        {form.ports.map((p, i) => (
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
                set(
                  "ports",
                  form.ports.map((x, j) => (j === i ? { ...x, port: Number(e.target.value) } : x)),
                )
              }
            />
            {(["tls", "http2", "proxy_protocol"] as const).map((k) => (
              <label key={k} className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  disabled={disabled || portForbidden.has(k)}
                  checked={p[k]}
                  onChange={(e) =>
                    set(
                      "ports",
                      form.ports.map((x, j) => (j === i ? { ...x, [k]: e.target.checked } : x)),
                    )
                  }
                />
                {k === "tls" ? "TLS" : k === "http2" ? "HTTP/2" : "PROXY"}
              </label>
            ))}
            <Button
              type="button"
              variant="ghost"
              size="sm"
              disabled={disabled || form.ports.length === 1}
              onClick={() =>
                set(
                  "ports",
                  form.ports.filter((_, j) => j !== i),
                )
              }
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
        <Button
          type="button"
          variant="outline"
          size="sm"
          disabled={disabled}
          onClick={() =>
            set("ports", [
              ...form.ports,
              {
                port: 80,
                tls: false,
                http2: false,
                proxy_protocol: false,
                https_redirect_port: null,
              },
            ])
          }
        >
          {t("Add port")}
        </Button>
      </section>

      {!forbidden.has("certificate_ids") && (
        <section className="space-y-4">
          <h2 className="text-lg font-semibold">{t("Certificates and policies")}</h2>
          <Field
            label={t("Certificates")}
            name="certificate_ids"
            changed={changed("certificate_ids")}
            live={liveText("certificate_ids")}
            onRevert={revert("certificate_ids")}
            errors={er("certificate_ids")}
          >
            <select
              multiple
              aria-label={t("Certificates")}
              className={cn(INPUT, "h-24")}
              disabled={disabled}
              value={form.certificate_ids}
              onChange={(e) =>
                set(
                  "certificate_ids",
                  [...e.target.selectedOptions].map((o) => o.value),
                )
              }
            >
              {certs.data?.items.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label={t("Policy sets")}
            name="policy_set_ids"
            changed={changed("policy_set_ids")}
            live={liveText("policy_set_ids")}
            onRevert={revert("policy_set_ids")}
            errors={er("policy_set_ids")}
          >
            <select
              multiple
              aria-label={t("Policy sets")}
              className={cn(INPUT, "h-24")}
              disabled={disabled}
              value={form.policy_set_ids}
              onChange={(e) =>
                set(
                  "policy_set_ids",
                  [...e.target.selectedOptions].map((o) => o.value),
                )
              }
            >
              {policies.data?.items.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
        </section>
      )}

      {!forbidden.has("routes") && (
        <section className="space-y-3">
          <h2 className="text-lg font-semibold">{t("Routes")}</h2>
          <Field
            label={t("Default pool")}
            name="default_pool_id"
            changed={changed("default_pool_id")}
            live={liveText("default_pool_id")}
            onRevert={revert("default_pool_id")}
            errors={er("default_pool_id")}
          >
            <select
              aria-label={t("Default pool")}
              className={INPUT}
              disabled={disabled}
              value={form.default_pool_id ?? ""}
              onChange={(e) => set("default_pool_id", e.target.value || null)}
            >
              <option value="">{t("None")}</option>
              {pools.data?.items.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
          {(errors.routes ?? []).map((m) => (
            <p key={m} role="alert" className="text-xs text-status-down">
              {m}
            </p>
          ))}
          {form.routes.map((r, i) => {
            const upd = (patchR: Partial<typeof r>) =>
              set(
                "routes",
                form.routes.map((x, j) => (j === i ? { ...x, ...patchR } : x)),
              );
            const redirect = r.redirect != null;
            return (
              <div
                key={i}
                className="grid gap-3 rounded-lg border bg-card p-3 text-sm md:grid-cols-4"
              >
                <input
                  aria-label={t("Route {n} host", { n: i + 1 })}
                  placeholder={t("host")}
                  className={INPUT}
                  disabled={disabled}
                  value={r.match.host ?? ""}
                  onChange={(e) => upd({ match: { ...r.match, host: e.target.value || null } })}
                />
                <input
                  aria-label={t("Route {n} path prefix", { n: i + 1 })}
                  placeholder={t("path prefix")}
                  className={cn(INPUT, "font-mono")}
                  disabled={disabled}
                  value={r.match.path_prefix ?? ""}
                  onChange={(e) =>
                    upd({ match: { ...r.match, path_prefix: e.target.value || null } })
                  }
                />
                {redirect ? (
                  <input
                    aria-label={t("Route {n} redirect location", { n: i + 1 })}
                    className={cn(INPUT, "font-mono")}
                    disabled={disabled}
                    value={r.redirect?.location ?? ""}
                    onChange={(e) =>
                      upd({ redirect: { location: e.target.value, code: r.redirect?.code ?? 301 } })
                    }
                  />
                ) : (
                  <select
                    aria-label={t("Route {n} pool", { n: i + 1 })}
                    className={INPUT}
                    disabled={disabled}
                    value={r.pool_id ?? ""}
                    onChange={(e) => upd({ pool_id: e.target.value || null })}
                  >
                    <option value="">{t("Pool")}</option>
                    {pools.data?.items.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                )}
                <div className="flex items-center gap-2">
                  <label className="flex items-center gap-1.5">
                    <input
                      type="checkbox"
                      disabled={disabled}
                      checked={redirect}
                      onChange={(e) =>
                        upd(
                          e.target.checked
                            ? { pool_id: null, redirect: { location: "/", code: 301 } }
                            : { redirect: null },
                        )
                      }
                    />
                    {t("Redirect")}
                  </label>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={disabled || i === 0}
                    aria-label={t("Move route {n} up", { n: i + 1 })}
                    onClick={() => set("routes", swap(form.routes, i, i - 1))}
                  >
                    ↑
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={disabled || i === form.routes.length - 1}
                    aria-label={t("Move route {n} down", { n: i + 1 })}
                    onClick={() => set("routes", swap(form.routes, i, i + 1))}
                  >
                    ↓
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={disabled}
                    onClick={() => set("routes", renumber(form.routes.filter((_, j) => j !== i)))}
                  >
                    {t("Remove")}
                  </Button>
                </div>
                {messagesFor(errors, `routes.${i}`).map((m) => (
                  <span key={m} role="alert" className="text-xs text-status-down md:col-span-4">
                    {m}
                  </span>
                ))}
              </div>
            );
          })}
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={disabled}
            onClick={() =>
              set("routes", [
                ...form.routes,
                {
                  order: form.routes.length + 1,
                  match: { host: null, path_prefix: null, path_exact: null },
                  pool_id: null,
                  redirect: null,
                },
              ])
            }
          >
            {t("Add route")}
          </Button>
        </section>
      )}

      <div className="sticky bottom-0 -mx-2 flex items-center gap-3 border-t bg-background px-2 py-3">
        <Tip label={write.reason}>
          <Button type="submit" disabled={!write.allowed || !dirty || saving || stale}>
            {t("Add to change set")}
          </Button>
        </Tip>
        <Button type="button" variant="ghost" disabled={!dirty} onClick={() => setForm(base)}>
          {t("Discard edits")}
        </Button>
        {dirty && (
          <span className="text-xs text-muted-foreground">
            {t("{n} unsaved fields", { n: Object.keys(patch).length })}
          </span>
        )}
      </div>
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
