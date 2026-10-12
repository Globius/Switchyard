/**
 * Alerts: what is firing now and the latest deliveries, the rules a tenant watches, and the
 * targets (webhook, chat, email) that are told. Rules and targets save live (no change set) and
 * are audited; changing them needs tenant-admin.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useMe } from "@/auth/session";
import { ChipInput } from "@/components/sy/ChipInput";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { shortWhen } from "@/lib/format";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Rule = components["schemas"]["AlertRuleRead"];
type Target = components["schemas"]["AlertTargetRead"];
type Catalog = components["schemas"]["AlertCatalog"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";
type Obj = Record<string, unknown>;
const str = (v: unknown) => (typeof v === "string" ? v : "");

function useAdmin(tenant: string) {
  const me = useMe().data;
  const role = me?.tenants.find((x) => x.name === tenant)?.role;
  const ok = !!me?.is_system || role === "tenant-admin" || role === "system-admin";
  return { ok, reason: ok ? undefined : t("Needs tenant-admin") };
}

/** One line for the rules table. */
export function ruleSummary(r: Pick<Rule, "kind" | "spec">): string {
  const s = r.spec as Obj;
  if (r.kind === "event")
    return t("on {a}", { a: (Array.isArray(s.actions) ? s.actions : []).join(", ") });
  if (r.kind === "metric")
    return `${str(s.metric)} ${s.op === "lt" ? "<" : ">"} ${String(s.threshold)} ${t("for {n} min", { n: Number(s.for_minutes ?? 0) })}${s.scope ? ` · ${str(s.scope)}` : ""}`;
  return t("{d} days before expiry", {
    d: (Array.isArray(s.days) ? s.days : [30, 14, 7]).join(", "),
  });
}

/** One line for the targets table. */
export function targetSummary(tg: Pick<Target, "kind" | "config">): string {
  const c = tg.config as Obj;
  if (tg.kind === "email") return (Array.isArray(c.to) ? c.to : []).join(", ");
  return `${str(c.url)}${tg.kind === "chat" && c.format ? ` · ${str(c.format)}` : ""}`;
}

function useCatalog() {
  return useQuery({
    queryKey: ["alert-catalog"],
    staleTime: 300_000,
    queryFn: async () => unwrap(await api.GET("/api/v1/alert-catalog")),
  });
}

function fieldErrors(e: unknown): Record<string, string> {
  const m: Record<string, string> = {};
  if (e instanceof ApiError && e.status === 422)
    for (const x of e.problem?.errors ?? []) m[x.path.split(".")[0] ?? x.path] = x.message;
  return m;
}

function RuleSheet({
  tenant,
  rule,
  catalog,
  targets,
  onClose,
}: {
  tenant: string;
  rule: Rule | null;
  catalog: Catalog;
  targets: Target[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = rule !== null;
  const [name, setName] = useState(rule?.name ?? "");
  const [kind, setKind] = useState<"event" | "metric" | "cert_expiry">(
    (rule?.kind as "event") ?? "event",
  );
  const spec0 = (rule?.spec ?? {}) as Obj;
  const [actions, setActions] = useState<string[]>(
    Array.isArray(spec0.actions) ? (spec0.actions as string[]) : [],
  );
  const [metric, setMetric] = useState(str(spec0.metric));
  const [op, setOp] = useState<"gt" | "lt">(spec0.op === "lt" ? "lt" : "gt");
  const [threshold, setThreshold] = useState(
    spec0.threshold != null ? String(spec0.threshold) : "",
  );
  const [forMin, setForMin] = useState(String(spec0.for_minutes ?? 5));
  const [days, setDays] = useState<string[]>(
    (Array.isArray(spec0.days) ? (spec0.days as number[]) : [30, 14, 7]).map(String),
  );
  const [picked, setPicked] = useState<string[]>(rule?.targets ?? []);
  const [enabled, setEnabled] = useState(rule?.enabled ?? true);
  const [problem, setProblem] = useState<unknown>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [confirmDelete, setConfirmDelete] = useState(false);

  const spec: Obj =
    kind === "event"
      ? { actions }
      : kind === "metric"
        ? { metric, op, threshold: Number(threshold), for_minutes: Number(forMin) }
        : { days: days.map(Number) };
  const m = catalog.metrics.find((x) => x.name === metric);
  const save = useMutation({
    mutationFn: async () => {
      setProblem(null);
      setErrors({});
      if (!rule)
        return unwrap(
          await api.POST("/api/v1/tenants/{t}/alert-rules", {
            params: { path: { t: tenant } },
            body: { name, kind, spec, targets: picked, enabled },
          }),
        );
      return unwrap(
        await api.PATCH("/api/v1/tenants/{t}/alert-rules/{name}", {
          params: { path: { t: tenant, name: rule.name } },
          body: { spec, targets: picked, enabled },
        }),
      );
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["t", tenant, "alerts"] });
      onClose();
    },
    onError: (e) => {
      const f = fieldErrors(e);
      if (Object.keys(f).length) setErrors(f);
      else setProblem(e);
    },
  });
  const remove = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.DELETE("/api/v1/tenants/{t}/alert-rules/{name}", {
          params: { path: { t: tenant, name: rule!.name } },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["t", tenant, "alerts"] });
      onClose();
    },
    onError: (e) => {
      setConfirmDelete(false);
      setProblem(e);
    },
  });
  const valid =
    name !== "" &&
    picked.length > 0 &&
    (kind === "event"
      ? actions.length > 0
      : kind === "metric"
        ? metric !== "" && threshold !== ""
        : days.length > 0);

  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={editing ? t("Edit {name}", { name: rule.name }) : t("Add alert rule")}
      badge={<StatusPill status="pending">{t("Live")}</StatusPill>}
      footer={
        <>
          {editing && (
            <Button variant="ghost" onClick={() => setConfirmDelete(true)}>
              {t("Delete")}
            </Button>
          )}
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button disabled={!valid || save.isPending} onClick={() => save.mutate()}>
            {editing ? t("Save") : t("Add rule")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Name")}
        <input
          aria-label={t("Name")}
          className={INPUT}
          disabled={editing}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        {errors.name && (
          <span role="alert" className="block text-xs font-normal text-status-down">
            {errors.name}
          </span>
        )}
      </label>
      <fieldset className="space-y-2" disabled={editing}>
        <legend className="text-sm font-medium">{t("Watch")}</legend>
        <div className="grid grid-cols-3 gap-2">
          {(
            [
              ["event", "An event", "An audit event, such as a failed apply."],
              ["metric", "A metric", "A series over or under a threshold."],
              ["cert_expiry", "Certificate expiry", "Certificates close to expiring."],
            ] as const
          ).map(([k, label, help]) => (
            <label
              key={k}
              className={cn(
                "flex cursor-pointer items-start gap-2 rounded-lg border bg-card p-2.5 text-sm",
                kind === k && "border-primary bg-primary/10",
              )}
            >
              <input
                type="radio"
                name="alert-kind"
                aria-label={label}
                className="mt-1"
                checked={kind === k}
                onChange={() => setKind(k)}
              />
              <span>
                <span className="font-medium">{t(label)}</span>
                <span className="block text-xs text-muted-foreground">{t(help)}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      {kind === "event" && (
        <div className="space-y-1.5">
          <div className="text-sm font-medium">{t("Audit actions")}</div>
          <ChipInput
            label={t("Audit actions")}
            value={actions}
            onChange={setActions}
            options={catalog.event_actions.map((a) => ({ value: a, label: a }))}
            placeholder={t("Add an action")}
          />
          {errors.spec && (
            <p role="alert" className="text-xs text-status-down">
              {errors.spec}
            </p>
          )}
        </div>
      )}
      {kind === "metric" && (
        <div className="space-y-3">
          <div className="space-y-1.5">
            <div className="text-sm font-medium">{t("Metric")}</div>
            <Combobox
              aria-label={t("Metric")}
              value={metric}
              onChange={setMetric}
              placeholder={t("Choose a metric")}
              options={catalog.metrics.map((x) => ({
                value: x.name,
                label: `${x.label} (${x.unit}, ${x.scope === "vs" ? t("per service") : t("per engine")})`,
              }))}
            />
          </div>
          <div className="grid grid-cols-3 gap-3">
            <label className="space-y-1.5 text-sm font-medium">
              {t("Fires when")}
              <select
                aria-label={t("Operator")}
                className={INPUT}
                value={op}
                onChange={(e) => setOp(e.target.value as "gt" | "lt")}
              >
                <option value="gt">{t("above")}</option>
                <option value="lt">{t("below")}</option>
              </select>
            </label>
            <label className="space-y-1.5 text-sm font-medium">
              {t("Threshold")}
              {m ? ` (${m.unit})` : ""}
              <input
                aria-label={t("Threshold")}
                type="number"
                className={INPUT}
                value={threshold}
                onChange={(e) => setThreshold(e.target.value)}
              />
            </label>
            <label className="space-y-1.5 text-sm font-medium">
              {t("For (minutes)")}
              <input
                aria-label={t("For minutes")}
                type="number"
                min={0}
                className={INPUT}
                value={forMin}
                onChange={(e) => setForMin(e.target.value)}
              />
            </label>
          </div>
          {errors.spec && (
            <p role="alert" className="text-xs text-status-down">
              {errors.spec}
            </p>
          )}
        </div>
      )}
      {kind === "cert_expiry" && (
        <div className="space-y-1.5">
          <div className="text-sm font-medium">{t("Days before expiry")}</div>
          <ChipInput
            label={t("Days before expiry")}
            value={days}
            onChange={setDays}
            freeText={{
              validate: (x) =>
                /^\d{1,3}$/.test(x.trim()) && Number(x) > 0 ? null : t("Use a number of days."),
            }}
            placeholder="30"
          />
          <p className="text-xs text-muted-foreground">
            {t("You are told at each of these, and again when it is renewed.")}
          </p>
        </div>
      )}
      <div className="space-y-1.5">
        <div className="text-sm font-medium">{t("Tell")}</div>
        <ChipInput
          label={t("Targets")}
          value={picked}
          onChange={setPicked}
          options={targets.map((x) => ({ value: x.name, label: x.name, hint: x.kind }))}
          emptyMessage={() => t("Add a target first, on the Targets tab.")}
          placeholder={t("Add a target")}
        />
        {errors.targets && (
          <p role="alert" className="text-xs text-status-down">
            {errors.targets}
          </p>
        )}
      </div>
      <label className="flex items-center gap-2 text-sm">
        <Switch aria-label={t("Enabled")} checked={enabled} onCheckedChange={setEnabled} />
        {t("Enabled")}
      </label>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete {name}?", { name: rule?.name ?? "" })}
        confirmLabel={t("Delete rule")}
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
      >
        <p>{t("The rule is removed at once, along with what it was firing. This is audited.")}</p>
      </ConfirmDialog>
    </Sheet>
  );
}

function TargetSheet({
  tenant,
  target,
  catalog,
  onClose,
}: {
  tenant: string;
  target: Target | null;
  catalog: Catalog;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const editing = target !== null;
  const c0 = (target?.config ?? {}) as Obj;
  const [name, setName] = useState(target?.name ?? "");
  const [kind, setKind] = useState<"webhook" | "chat" | "email">(
    (target?.kind as "webhook") ?? "webhook",
  );
  const [url, setUrl] = useState(str(c0.url));
  const [format, setFormat] = useState(str(c0.format) || catalog.chat_formats[0] || "slack");
  const [to, setTo] = useState<string[]>(Array.isArray(c0.to) ? (c0.to as string[]) : []);
  const [secret, setSecret] = useState(target?.secret_ref ?? "");
  const [enabled, setEnabled] = useState(target?.enabled ?? true);
  const [problem, setProblem] = useState<unknown>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [test, setTest] = useState<{ ok: boolean; error?: string | null } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const config: Obj = kind === "email" ? { to } : kind === "chat" ? { url, format } : { url };

  const save = useMutation({
    mutationFn: async () => {
      setProblem(null);
      setErrors({});
      if (!target)
        return unwrap(
          await api.POST("/api/v1/tenants/{t}/alert-targets", {
            params: { path: { t: tenant } },
            body: {
              name,
              kind,
              config,
              enabled,
              secret_ref: kind === "webhook" && secret ? secret : null,
            },
          }),
        );
      return unwrap(
        await api.PATCH("/api/v1/tenants/{t}/alert-targets/{name}", {
          params: { path: { t: tenant, name: target.name } },
          body: { config, enabled, secret_ref: kind === "webhook" ? secret || null : null },
        }),
      );
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["t", tenant, "alerts"] });
      onClose();
    },
    onError: (e) => {
      const f = fieldErrors(e);
      if (Object.keys(f).length) setErrors(f);
      else setProblem(e);
    },
  });
  const runTest = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/alert-targets/{name}/test", {
          params: { path: { t: tenant, name: target!.name } },
        }),
      ),
    onSuccess: (r) => setTest(r),
    onError: setProblem,
  });
  const remove = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.DELETE("/api/v1/tenants/{t}/alert-targets/{name}", {
          params: { path: { t: tenant, name: target!.name } },
        }),
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["t", tenant, "alerts"] });
      onClose();
    },
    onError: (e) => {
      setConfirmDelete(false);
      setProblem(e);
    },
  });
  const valid = name !== "" && (kind === "email" ? to.length > 0 : url !== "");

  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={editing ? t("Edit {name}", { name: target.name }) : t("Add alert target")}
      badge={<StatusPill status="pending">{t("Live")}</StatusPill>}
      footer={
        <>
          {editing && (
            <Button variant="ghost" onClick={() => setConfirmDelete(true)}>
              {t("Delete")}
            </Button>
          )}
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button disabled={!valid || save.isPending} onClick={() => save.mutate()}>
            {editing ? t("Save") : t("Add target")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
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
          {t("Lowercase letters, digits, - and _.")}
        </span>
        {errors.name && (
          <span role="alert" className="block text-xs font-normal text-status-down">
            {errors.name}
          </span>
        )}
      </label>
      <fieldset className="space-y-1.5" disabled={editing}>
        <legend className="text-sm font-medium">{t("Type")}</legend>
        <div role="radiogroup" className="flex gap-5 text-sm">
          {(
            [
              ["webhook", "Webhook"],
              ["chat", "Chat (Slack, Teams, Mattermost)"],
              ["email", "Email"],
            ] as const
          ).map(([k, label]) => (
            <label key={k} className="flex items-center gap-1.5">
              <input
                type="radio"
                name="target-kind"
                aria-label={label}
                checked={kind === k}
                onChange={() => setKind(k)}
              />
              {t(label)}
            </label>
          ))}
        </div>
      </fieldset>
      {kind !== "email" && (
        <label className="block space-y-1.5 text-sm font-medium">
          {t("URL")}
          <input
            aria-label={t("URL")}
            className={cn(INPUT, "font-mono")}
            placeholder="https://"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          <span className="block text-xs font-normal text-muted-foreground">
            {t("https only; private addresses are refused.")}
          </span>
          {errors.config && (
            <span role="alert" className="block text-xs font-normal text-status-down">
              {errors.config}
            </span>
          )}
        </label>
      )}
      {kind === "chat" && (
        <label className="block space-y-1.5 text-sm font-medium">
          {t("Format")}
          <select
            aria-label={t("Format")}
            className={INPUT}
            value={format}
            onChange={(e) => setFormat(e.target.value)}
          >
            {catalog.chat_formats.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        </label>
      )}
      {kind === "webhook" && (
        <label className="block space-y-1.5 text-sm font-medium">
          {t("Signing secret (optional)")}
          <input
            aria-label={t("Signing secret")}
            className={cn(INPUT, "font-mono")}
            placeholder="kv/switchyard/…"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
          />
          <span className="block text-xs font-normal text-muted-foreground">
            {t(
              "An OpenBao path whose value signs each body (X-Switchyard-Signature: sha256=…). The secret itself is never stored here.",
            )}
          </span>
          {errors.secret_ref && (
            <span role="alert" className="block text-xs font-normal text-status-down">
              {errors.secret_ref}
            </span>
          )}
        </label>
      )}
      {kind === "email" && (
        <div className="space-y-1.5">
          <div className="text-sm font-medium">{t("Recipients")}</div>
          <ChipInput
            label={t("Recipients")}
            value={to}
            onChange={setTo}
            freeText={{
              validate: (x) =>
                /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(x.trim()) ? null : t("Use an email address."),
            }}
            placeholder="ops@example.com"
          />
          {!catalog.email_configured && (
            <p role="note" className="text-xs text-status-degraded">
              {t(
                "This controller has no email server configured, so email targets cannot send yet.",
              )}
            </p>
          )}
        </div>
      )}
      <label className="flex items-center gap-2 text-sm">
        <Switch aria-label={t("Enabled")} checked={enabled} onCheckedChange={setEnabled} />
        {t("Enabled")}
      </label>
      {editing && (
        <div className="space-y-2">
          <Button variant="outline" disabled={runTest.isPending} onClick={() => runTest.mutate()}>
            {t("Send a test message")}
          </Button>
          {test && (
            <p role="status" className="text-sm">
              {test.ok ? t("Delivered.") : t("Not delivered: {why}", { why: test.error ?? "" })}
            </p>
          )}
        </div>
      )}
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete {name}?", { name: target?.name ?? "" })}
        confirmLabel={t("Delete target")}
        busy={remove.isPending}
        onConfirm={() => remove.mutate()}
      >
        <p>{t("Rules that name it skip it from then on. This is audited.")}</p>
      </ConfirmDialog>
    </Sheet>
  );
}

export function Alerts({ tenant }: { tenant: string }) {
  const admin = useAdmin(tenant);
  const catalog = useCatalog();
  const status = useQuery({
    queryKey: ["t", tenant, "alerts", "status"],
    refetchInterval: 15_000,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/alerts", {
          params: { path: { t: tenant }, query: { limit: 30 } },
        }),
      ),
  });
  const rules = useQuery({
    queryKey: ["t", tenant, "alerts", "rules"],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/alert-rules", { params: { path: { t: tenant } } })),
  });
  const targets = useQuery({
    queryKey: ["t", tenant, "alerts", "targets"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/alert-targets", { params: { path: { t: tenant } } }),
      ),
  });
  const [tab, setTab] = useState<"status" | "rules" | "targets">("status");
  const [sheet, setSheet] = useState<
    { kind: "rule"; v: Rule | null } | { kind: "target"; v: Target | null } | null
  >(null);
  const firing = status.data?.firing ?? [];

  const head = (cols: string[]) => (
    <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
      <tr>
        {cols.map((h) => (
          <th key={h} className="px-4 py-2.5 font-medium">
            {t(h)}
          </th>
        ))}
      </tr>
    </thead>
  );

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("Alerts")}</h1>
          <p className="text-sm text-muted-foreground">
            {t(
              "What is firing, what {tenant} watches, and who is told. Changes apply at once and are audited.",
              { tenant },
            )}
          </p>
        </div>
        {tab !== "status" && (
          <Tip label={admin.reason}>
            <Button
              disabled={!admin.ok || !catalog.data}
              onClick={() =>
                setSheet({ kind: tab === "rules" ? "rule" : "target", v: null } as never)
              }
            >
              {tab === "rules" ? t("Add rule") : t("Add target")}
            </Button>
          </Tip>
        )}
      </div>
      <div role="tablist" aria-label={t("Alerts")} className="flex gap-1 border-b">
        {(
          [
            ["status", "Status"],
            ["rules", "Rules"],
            ["targets", "Targets"],
          ] as const
        ).map(([k, label]) => (
          <button
            key={k}
            role="tab"
            aria-selected={tab === k}
            onClick={() => setTab(k)}
            className={cn(
              "border-b-2 px-4 py-2 text-sm",
              tab === k ? "border-primary font-medium" : "border-transparent text-muted-foreground",
            )}
          >
            {t(label)}
            {k === "status" && firing.length > 0 && (
              <span className="ml-1.5">
                <StatusPill status="down">{firing.length}</StatusPill>
              </span>
            )}
          </button>
        ))}
      </div>

      {tab === "status" && (
        <div className="space-y-5">
          <section className="overflow-hidden rounded-lg border bg-card">
            <header className="border-b px-4 py-3 text-base font-semibold">
              {t("Firing now")}
            </header>
            {status.isPending ? (
              <TableSkeleton rows={2} cols={4} />
            ) : status.isError ? (
              <ErrorState error={status.error} onRetry={() => void status.refetch()} />
            ) : firing.length === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">{t("Nothing is firing.")}</p>
            ) : (
              <table className="w-full text-[13px]">
                {head(["Rule", "Watching", "State", "Since"])}
                <tbody className="divide-y">
                  {firing.map((f) => (
                    <tr key={`${f.rule}-${f.key}`}>
                      <td className="px-4 py-2.5 font-medium">{f.rule}</td>
                      <td className="px-4 py-2.5 font-mono text-xs break-all">{f.key}</td>
                      <td className="px-4 py-2.5">
                        <StatusPill status={f.state === "firing" ? "down" : "degraded"}>
                          {f.state === "firing" ? t("Firing") : t("Pending")}
                        </StatusPill>
                      </td>
                      <td className="px-4 py-2.5 text-muted-foreground">{shortWhen(f.since)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
          <section className="overflow-hidden rounded-lg border bg-card">
            <header className="border-b px-4 py-3 text-base font-semibold">
              {t("Latest deliveries")}
            </header>
            {(status.data?.deliveries ?? []).length === 0 ? (
              <p className="p-4 text-sm text-muted-foreground">{t("Nothing has been sent yet.")}</p>
            ) : (
              <table className="w-full text-[13px]">
                {head(["When", "Rule", "Target", "Kind", "Result"])}
                <tbody className="divide-y">
                  {status.data!.deliveries.map((d, i) => (
                    <tr key={i}>
                      <td className="px-4 py-2.5 whitespace-nowrap text-muted-foreground">
                        {shortWhen(d.at)}
                      </td>
                      <td className="px-4 py-2.5">{d.rule ?? "—"}</td>
                      <td className="px-4 py-2.5">{d.target}</td>
                      <td className="px-4 py-2.5">{d.kind}</td>
                      <td className="px-4 py-2.5">
                        {d.status === "sent" ? (
                          <StatusPill status="healthy">{t("Sent")}</StatusPill>
                        ) : (
                          <>
                            <StatusPill status="down">{t("Failed")}</StatusPill>
                            {d.error && (
                              <span className="block font-mono text-xs break-words text-muted-foreground">
                                {d.error}
                              </span>
                            )}
                          </>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </div>
      )}

      {tab === "rules" && (
        <div className="overflow-x-auto rounded-lg border bg-card">
          {rules.isPending ? (
            <TableSkeleton cols={5} />
          ) : rules.isError ? (
            <ErrorState error={rules.error} onRetry={() => void rules.refetch()} />
          ) : rules.data.length === 0 ? (
            <EmptyState
              title={t("No alert rules yet")}
              body={t("Watch an audit event, a metric or certificate expiry, and tell a target.")}
            />
          ) : (
            <table className="w-full text-[13px]">
              {head(["Name", "Watches", "Tells", "Status"])}
              <tbody className="divide-y">
                {rules.data.map((r) => (
                  <tr key={r.id}>
                    <td className="px-4 py-3">
                      <button
                        type="button"
                        className="font-medium text-primary hover:underline"
                        onClick={() => setSheet({ kind: "rule", v: r })}
                      >
                        {r.name}
                      </button>
                    </td>
                    <td className="px-4 py-3">
                      <span className="block text-xs text-muted-foreground">{r.kind}</span>
                      {ruleSummary(r)}
                    </td>
                    <td className="px-4 py-3">{r.targets.join(", ")}</td>
                    <td className="px-4 py-3">
                      {r.enabled ? (
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
      )}

      {tab === "targets" && (
        <div className="overflow-x-auto rounded-lg border bg-card">
          {targets.isPending ? (
            <TableSkeleton cols={4} />
          ) : targets.isError ? (
            <ErrorState error={targets.error} onRetry={() => void targets.refetch()} />
          ) : targets.data.length === 0 ? (
            <EmptyState
              title={t("No alert targets yet")}
              body={t("Add a webhook, a chat channel or email addresses.")}
            />
          ) : (
            <table className="w-full text-[13px]">
              {head(["Name", "Type", "Destination", "Status"])}
              <tbody className="divide-y">
                {targets.data.map((x) => (
                  <tr key={x.id}>
                    <td className="px-4 py-3">
                      <button
                        type="button"
                        className="font-medium text-primary hover:underline"
                        onClick={() => setSheet({ kind: "target", v: x })}
                      >
                        {x.name}
                      </button>
                    </td>
                    <td className="px-4 py-3">{x.kind}</td>
                    <td className="px-4 py-3 font-mono text-xs break-all">{targetSummary(x)}</td>
                    <td className="px-4 py-3">
                      {x.enabled ? (
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
      )}

      {sheet?.kind === "rule" && catalog.data && (
        <RuleSheet
          key={sheet.v?.id ?? "new"}
          tenant={tenant}
          rule={sheet.v}
          catalog={catalog.data}
          targets={targets.data ?? []}
          onClose={() => setSheet(null)}
        />
      )}
      {sheet?.kind === "target" && catalog.data && (
        <TargetSheet
          key={sheet.v?.id ?? "new"}
          tenant={tenant}
          target={sheet.v}
          catalog={catalog.data}
          onClose={() => setSheet(null)}
        />
      )}
    </div>
  );
}
