/**
 * Alerts (ADR 0120): where a tenant's alerts go, what they watch, what is firing and what was sent.
 *
 * There is no artboard for this screen yet (docs/design/07-ui-vs-artboards.md): it reuses the
 * shared table, sheet and state components and should be redrawn by Design. Everything here applies
 * live (no change set), and only tenant administrators can change anything.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { shortWhen } from "@/lib/format";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Target = components["schemas"]["AlertTargetRead"];
type Rule = components["schemas"]["AlertRuleRead"];
type Catalog = components["schemas"]["AlertCatalog"];

const INPUT = "h-9 w-full rounded-md border border-input bg-card px-3 text-sm";
const CARD = "overflow-hidden rounded-lg border bg-card";

function useAlerts(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "alerts"],
    refetchInterval: 30_000,
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/alerts", { params: { path: { t: tenant } } })),
  });
}
function useTargets(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "alert-targets"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/alert-targets", { params: { path: { t: tenant } } }),
      ),
  });
}
function useRules(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "alert-rules"],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/alert-rules", { params: { path: { t: tenant } } })),
  });
}
function useCatalog() {
  return useQuery({
    queryKey: ["alert-catalog"],
    queryFn: async () => unwrap(await api.GET("/api/v1/alert-catalog")),
  });
}

function hostOf(url: string | undefined): string {
  try {
    return new URL(url ?? "").host;
  } catch {
    return "";
  }
}

/** One line that says what a target does, without showing its URL (chat URLs are secrets). */
export function targetSummary(t_: Pick<Target, "kind" | "config">): string {
  const c = t_.config as { url?: string; format?: string; to?: string[] };
  if (t_.kind === "email") return (c.to ?? []).join(", ");
  const host = hostOf(c.url);
  return t_.kind === "chat" ? `${c.format ?? "slack"} · ${host}` : host;
}

/** One line that says what a rule watches. */
export function ruleSummary(r: Pick<Rule, "kind" | "spec">): string {
  const s = r.spec as {
    actions?: string[];
    metric?: string;
    op?: string;
    threshold?: number;
    for_minutes?: number;
    scope?: string;
    days?: number[];
  };
  if (r.kind === "event") return (s.actions ?? []).join(", ");
  if (r.kind === "cert_expiry")
    return t("{days} days before expiry", { days: (s.days ?? []).join(", ") });
  return t("{metric} {op} {value} for {min} min{scope}", {
    metric: s.metric ?? "",
    op: s.op === "lt" ? "<" : ">",
    value: s.threshold ?? "",
    min: s.for_minutes ?? 0,
    scope: s.scope ? ` · ${s.scope}` : "",
  });
}

function useInvalidate(tenant: string) {
  const qc = useQueryClient();
  return () => {
    void qc.invalidateQueries({ queryKey: ["t", tenant, "alert-targets"] });
    void qc.invalidateQueries({ queryKey: ["t", tenant, "alert-rules"] });
    void qc.invalidateQueries({ queryKey: ["t", tenant, "alerts"] });
  };
}

function TargetSheet({
  tenant,
  catalog,
  onClose,
}: {
  tenant: string;
  catalog: Catalog | undefined;
  onClose: () => void;
}) {
  const refresh = useInvalidate(tenant);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"webhook" | "chat" | "email">("chat");
  const [url, setUrl] = useState("");
  const [format, setFormat] = useState("slack");
  const [to, setTo] = useState("");
  const [secretRef, setSecretRef] = useState("");
  const create = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/alert-targets", {
          params: { path: { t: tenant } },
          body: {
            name,
            kind,
            config:
              kind === "email"
                ? { to: to.split(/[\s,]+/).filter(Boolean) }
                : kind === "chat"
                  ? { url, format }
                  : { url },
            secret_ref: kind === "webhook" && secretRef ? secretRef : null,
            enabled: true,
          },
        }),
      ),
    onSuccess: () => {
      refresh();
      onClose();
    },
  });
  return (
    <Sheet
      open
      centered
      onOpenChange={(o) => !o && onClose()}
      title={t("Add alert target")}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button disabled={!name || create.isPending} onClick={() => create.mutate()}>
            {t("Add target")}
          </Button>
        </>
      }
    >
      {create.error && <ErrorState error={create.error} />}
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Name")}
        <input
          aria-label={t("Name")}
          className={cn(INPUT, "font-mono")}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Kind")}
        <select
          aria-label={t("Kind")}
          className={INPUT}
          value={kind}
          onChange={(e) => setKind(e.target.value as typeof kind)}
        >
          <option value="chat">{t("Chat (Slack, Teams, Mattermost)")}</option>
          <option value="webhook">{t("Webhook")}</option>
          <option value="email" disabled={catalog ? !catalog.email_configured : false}>
            {t("Email")}
            {catalog && !catalog.email_configured ? ` (${t("no mail server configured")})` : ""}
          </option>
        </select>
      </label>
      {kind !== "email" && (
        <label className="block space-y-1.5 text-sm font-medium">
          {t("URL")}
          <input
            aria-label={t("URL")}
            className={cn(INPUT, "font-mono")}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://"
          />
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
            {(catalog?.chat_formats ?? ["slack", "teams", "mattermost"]).map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        </label>
      )}
      {kind === "webhook" && (
        <label className="block space-y-1.5 text-sm font-medium">
          {t("Signing secret (OpenBao path, optional)")}
          <input
            aria-label={t("Signing secret")}
            className={cn(INPUT, "font-mono")}
            value={secretRef}
            onChange={(e) => setSecretRef(e.target.value)}
            placeholder="kv/tenants/…/alerts"
          />
        </label>
      )}
      {kind === "email" && (
        <label className="block space-y-1.5 text-sm font-medium">
          {t("Addresses")}
          <input
            aria-label={t("Addresses")}
            className={INPUT}
            value={to}
            onChange={(e) => setTo(e.target.value)}
            placeholder="ops@example.com, oncall@example.com"
          />
        </label>
      )}
      <p className="text-xs text-muted-foreground">
        {t(
          "Addresses must be public: the controller refuses URLs that name private or local hosts.",
        )}
      </p>
    </Sheet>
  );
}

function RuleSheet({
  tenant,
  targets,
  catalog,
  onClose,
}: {
  tenant: string;
  targets: Target[];
  catalog: Catalog | undefined;
  onClose: () => void;
}) {
  const refresh = useInvalidate(tenant);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"cert_expiry" | "metric" | "event">("cert_expiry");
  const [days, setDays] = useState("30, 14, 7");
  const [metric, setMetric] = useState("vs_5xx_ratio");
  const [op, setOp] = useState("gt");
  const [threshold, setThreshold] = useState("5");
  const [forMinutes, setForMinutes] = useState("5");
  const [scope, setScope] = useState("");
  const [actions, setActions] = useState<string[]>([]);
  const [chosen, setChosen] = useState<string[]>(targets[0] ? [targets[0].name] : []);
  const spec =
    kind === "cert_expiry"
      ? {
          days: days
            .split(/[\s,]+/)
            .filter(Boolean)
            .map(Number),
        }
      : kind === "metric"
        ? {
            metric,
            op,
            threshold: Number(threshold),
            for_minutes: Number(forMinutes),
            ...(scope ? { scope } : {}),
          }
        : { actions };
  const create = useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/alert-rules", {
          params: { path: { t: tenant } },
          body: { name, kind, spec, targets: chosen, enabled: true },
        }),
      ),
    onSuccess: () => {
      refresh();
      onClose();
    },
  });
  const toggle = (list: string[], v: string) =>
    list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={t("Add alert rule")}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            disabled={!name || chosen.length === 0 || create.isPending}
            onClick={() => create.mutate()}
          >
            {t("Add rule")}
          </Button>
        </>
      }
    >
      {create.error && <ErrorState error={create.error} />}
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Name")}
        <input
          aria-label={t("Name")}
          className={cn(INPUT, "font-mono")}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Watch")}
        <select
          aria-label={t("Watch")}
          className={INPUT}
          value={kind}
          onChange={(e) => setKind(e.target.value as typeof kind)}
        >
          <option value="cert_expiry">{t("Certificate expiry")}</option>
          <option value="metric">{t("A metric")}</option>
          <option value="event">{t("Events (audit log)")}</option>
        </select>
      </label>
      {kind === "cert_expiry" && (
        <label className="block space-y-1.5 text-sm font-medium">
          {t("Days before expiry")}
          <input
            aria-label={t("Days before expiry")}
            className={INPUT}
            value={days}
            onChange={(e) => setDays(e.target.value)}
          />
        </label>
      )}
      {kind === "metric" && (
        <div className="grid grid-cols-2 gap-3">
          <label className="col-span-2 block space-y-1.5 text-sm font-medium">
            {t("Metric")}
            <select
              aria-label={t("Metric")}
              className={INPUT}
              value={metric}
              onChange={(e) => setMetric(e.target.value)}
            >
              {(catalog?.metrics ?? []).map((m) => (
                <option key={m.name} value={m.name}>
                  {m.label} ({m.unit})
                </option>
              ))}
            </select>
          </label>
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Alert when")}
            <select
              aria-label={t("Alert when")}
              className={INPUT}
              value={op}
              onChange={(e) => setOp(e.target.value)}
            >
              <option value="gt">{t("above")}</option>
              <option value="lt">{t("below")}</option>
            </select>
          </label>
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Threshold")}
            <input
              aria-label={t("Threshold")}
              className={INPUT}
              value={threshold}
              onChange={(e) => setThreshold(e.target.value)}
            />
          </label>
          <label className="block space-y-1.5 text-sm font-medium">
            {t("For (minutes)")}
            <input
              aria-label={t("For (minutes)")}
              className={INPUT}
              value={forMinutes}
              onChange={(e) => setForMinutes(e.target.value)}
            />
          </label>
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Only this service or engine")}
            <input
              aria-label={t("Only this service or engine")}
              className={cn(INPUT, "font-mono")}
              value={scope}
              onChange={(e) => setScope(e.target.value)}
            />
          </label>
        </div>
      )}
      {kind === "event" && (
        <fieldset className="max-h-48 space-y-1 overflow-auto rounded-md border p-3">
          <legend className="px-1 text-sm font-medium">{t("Audit actions")}</legend>
          {(catalog?.event_actions ?? []).map((a) => (
            <label key={a} className="flex items-center gap-2 font-mono text-xs">
              <input
                type="checkbox"
                checked={actions.includes(a)}
                onChange={() => setActions(toggle(actions, a))}
              />
              {a}
            </label>
          ))}
        </fieldset>
      )}
      <fieldset className="space-y-1 rounded-md border p-3">
        <legend className="px-1 text-sm font-medium">{t("Tell")}</legend>
        {targets.length === 0 && (
          <p className="text-sm text-muted-foreground">{t("Add a target first.")}</p>
        )}
        {targets.map((x) => (
          <label key={x.id} className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={chosen.includes(x.name)}
              onChange={() => setChosen(toggle(chosen, x.name))}
            />
            {x.name}
          </label>
        ))}
      </fieldset>
    </Sheet>
  );
}

export function Alerts({ tenant }: { tenant: string }) {
  const can = useCan(tenant);
  const write = can("write", "infrastructure");
  const refresh = useInvalidate(tenant);
  const status = useAlerts(tenant);
  const targets = useTargets(tenant);
  const rules = useRules(tenant);
  const catalog = useCatalog();
  const [adding, setAdding] = useState<"target" | "rule" | null>(null);
  const [testResult, setTestResult] = useState<Record<string, string>>({});

  const test = useMutation({
    mutationFn: async (name: string) =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/alert-targets/{name}/test", {
          params: { path: { t: tenant, name } },
        }),
      ),
    onSuccess: (r, name) => {
      setTestResult((p) => ({ ...p, [name]: r.ok ? t("Delivered") : (r.error ?? t("Failed")) }));
      refresh();
    },
  });
  const toggleTarget = useMutation({
    mutationFn: async (x: Target) =>
      unwrap(
        await api.PATCH("/api/v1/tenants/{t}/alert-targets/{name}", {
          params: { path: { t: tenant, name: x.name } },
          body: { enabled: !x.enabled },
        }),
      ),
    onSuccess: refresh,
  });
  const toggleRule = useMutation({
    mutationFn: async (x: Rule) =>
      unwrap(
        await api.PATCH("/api/v1/tenants/{t}/alert-rules/{name}", {
          params: { path: { t: tenant, name: x.name } },
          body: { enabled: !x.enabled },
        }),
      ),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: async (a: { kind: "target" | "rule"; name: string }) =>
      a.kind === "target"
        ? unwrap(
            await api.DELETE("/api/v1/tenants/{t}/alert-targets/{name}", {
              params: { path: { t: tenant, name: a.name } },
            }),
          )
        : unwrap(
            await api.DELETE("/api/v1/tenants/{t}/alert-rules/{name}", {
              params: { path: { t: tenant, name: a.name } },
            }),
          ),
    onSuccess: refresh,
  });

  return (
    <div className="space-y-6 p-6">
      <header>
        <h1 className="text-xl font-semibold">{t("Alerts")}</h1>
        <p className="text-sm text-muted-foreground">
          {t(
            "Tell people when a certificate is about to expire, an event happens or a metric is out of range. Changes apply at once.",
          )}
        </p>
      </header>

      <section className={CARD}>
        <header className="flex items-center gap-3 border-b px-4 py-3">
          <h2 className="text-base font-semibold">{t("Firing now")}</h2>
        </header>
        {status.isPending ? (
          <TableSkeleton rows={2} cols={4} />
        ) : status.isError ? (
          <ErrorState error={status.error} onRetry={() => void status.refetch()} />
        ) : status.data.firing.length === 0 ? (
          <EmptyState
            title={t("Nothing is firing")}
            body={t("Alerts that are firing or waiting out their duration show here.")}
          />
        ) : (
          <table className="w-full text-[13px]">
            <tbody className="divide-y">
              {status.data.firing.map((f) => (
                <tr key={`${f.rule}/${f.key}`}>
                  <td className="px-4 py-2.5">
                    <StatusPill status={f.state === "firing" ? "down" : "degraded"}>
                      {f.state === "firing" ? t("Firing") : t("Pending")}
                    </StatusPill>
                  </td>
                  <td className="px-4 py-2.5 font-mono">{f.rule}</td>
                  <td className="px-4 py-2.5 font-mono text-xs">{f.key}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">{shortWhen(f.since)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className={CARD}>
        <header className="flex items-center gap-3 border-b px-4 py-3">
          <h2 className="text-base font-semibold">{t("Targets")}</h2>
          <Tip label={write.reason}>
            <Button
              className="ml-auto"
              disabled={!write.allowed}
              onClick={() => setAdding("target")}
            >
              {t("Add target")}
            </Button>
          </Tip>
        </header>
        {targets.isPending ? (
          <TableSkeleton rows={2} cols={4} />
        ) : targets.isError ? (
          <ErrorState error={targets.error} onRetry={() => void targets.refetch()} />
        ) : targets.data.length === 0 ? (
          <EmptyState
            title={t("No targets")}
            body={t("Add a Slack, Teams, Mattermost, webhook or email target to receive alerts.")}
          />
        ) : (
          <table className="w-full text-[13px]">
            <tbody className="divide-y">
              {targets.data.map((x) => (
                <tr key={x.id}>
                  <td className="px-4 py-2.5 font-mono">{x.name}</td>
                  <td className="px-4 py-2.5">{x.kind}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">{targetSummary(x)}</td>
                  <td className="px-4 py-2.5">
                    <Switch
                      aria-label={t("Enabled")}
                      checked={x.enabled}
                      disabled={!write.allowed}
                      onCheckedChange={() => toggleTarget.mutate(x)}
                    />
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <span className="mr-3 text-xs text-muted-foreground">{testResult[x.name]}</span>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={!write.allowed || test.isPending}
                      onClick={() => test.mutate(x.name)}
                    >
                      {t("Send test")}
                    </Button>{" "}
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={!write.allowed}
                      onClick={() => remove.mutate({ kind: "target", name: x.name })}
                    >
                      {t("Delete")}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className={CARD}>
        <header className="flex items-center gap-3 border-b px-4 py-3">
          <h2 className="text-base font-semibold">{t("Rules")}</h2>
          <Tip label={write.reason}>
            <Button
              className="ml-auto"
              disabled={!write.allowed || (targets.data?.length ?? 0) === 0}
              onClick={() => setAdding("rule")}
            >
              {t("Add rule")}
            </Button>
          </Tip>
        </header>
        {rules.isPending ? (
          <TableSkeleton rows={2} cols={4} />
        ) : rules.isError ? (
          <ErrorState error={rules.error} onRetry={() => void rules.refetch()} />
        ) : rules.data.length === 0 ? (
          <EmptyState
            title={t("No rules")}
            body={t("A rule says what to watch and which targets to tell.")}
          />
        ) : (
          <table className="w-full text-[13px]">
            <tbody className="divide-y">
              {rules.data.map((x) => (
                <tr key={x.id}>
                  <td className="px-4 py-2.5 font-mono">{x.name}</td>
                  <td className="px-4 py-2.5 text-muted-foreground">{ruleSummary(x)}</td>
                  <td className="px-4 py-2.5">{x.targets.join(", ")}</td>
                  <td className="px-4 py-2.5">
                    <Switch
                      aria-label={t("Enabled")}
                      checked={x.enabled}
                      disabled={!write.allowed}
                      onCheckedChange={() => toggleRule.mutate(x)}
                    />
                  </td>
                  <td className="px-4 py-2.5 text-right">
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={!write.allowed}
                      onClick={() => remove.mutate({ kind: "rule", name: x.name })}
                    >
                      {t("Delete")}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className={CARD}>
        <header className="border-b px-4 py-3">
          <h2 className="text-base font-semibold">{t("Recent deliveries")}</h2>
        </header>
        {status.data && status.data.deliveries.length > 0 ? (
          <table className="w-full text-[13px]">
            <tbody className="divide-y">
              {status.data.deliveries.map((d, i) => (
                <tr key={i}>
                  <td className="px-4 py-2.5 text-muted-foreground">{shortWhen(d.at)}</td>
                  <td className="px-4 py-2.5">
                    <StatusPill status={d.status === "sent" ? "healthy" : "down"}>
                      {d.status === "sent" ? t("Sent") : t("Failed")}
                    </StatusPill>
                  </td>
                  <td className="px-4 py-2.5">{d.kind}</td>
                  <td className="px-4 py-2.5 font-mono">{d.target}</td>
                  <td className="px-4 py-2.5 font-mono text-xs">{d.key}</td>
                  <td className="px-4 py-2.5 text-xs text-status-down">{d.error}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <EmptyState
            title={t("Nothing sent yet")}
            body={t("Each message the controller sends, and whether it arrived, is listed here.")}
          />
        )}
      </section>

      {adding === "target" && (
        <TargetSheet tenant={tenant} catalog={catalog.data} onClose={() => setAdding(null)} />
      )}
      {adding === "rule" && (
        <RuleSheet
          tenant={tenant}
          targets={targets.data ?? []}
          catalog={catalog.data}
          onClose={() => setAdding(null)}
        />
      )}
    </div>
  );
}
