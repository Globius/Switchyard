// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useMutation, useQuery } from "@tanstack/react-query";
import { hostPort } from "@/lib/ip";
import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { ChipInput } from "@/components/sy/ChipInput";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { DraftMarker } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Tip } from "@/components/ui/tooltip";
import { useSaveOp } from "@/features/objects/save";
import { useDraft } from "@/features/shell/draft";
import { usePools } from "@/features/vs/detail-queries";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Monitor = components["schemas"]["HealthMonitorRead"];
export interface MonitorForm {
  name: string;
  type: "tcp" | "http" | "udp" | "grpc";
  interval_s: number;
  timeout_s: number;
  rise: number;
  fall: number;
  port: number | null;
  http: {
    method: string;
    path: string;
    host: string | null;
    expect_status: number[];
    expect_body?: string | null;
    expect_header?: { name: string; value: string | null } | null;
  } | null;
  /** A built-in protocol check for a tcp monitor (HAProxy's own, no custom commands). */
  preset?: "redis" | "smtp" | "ldap" | "mysql" | null;
  /** Server name the check presents to a TLS pool (check-sni). */
  sni?: string | null;
  udp: { payload_hex: string | null; require_reply: boolean } | null;
  /** gRPC health probe: an HTTP/2 request to the standard health method (ADR 0227). */
  grpc?: { service: string; expect_status: number[] } | null;
}

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function emptyMonitor(): MonitorForm {
  return {
    name: "",
    type: "tcp",
    interval_s: 5,
    timeout_s: 2,
    rise: 2,
    fall: 3,
    port: null,
    http: null,
    udp: null,
    grpc: null,
  };
}

export function toForm(m: Monitor): MonitorForm {
  return {
    name: m.name,
    type: m.type,
    interval_s: m.interval_s,
    timeout_s: m.timeout_s,
    rise: m.rise,
    fall: m.fall,
    port: m.port ?? null,
    http: m.http
      ? {
          method: m.http.method ?? "GET",
          path: m.http.path ?? "/",
          host: m.http.host ?? null,
          expect_status: m.http.expect_status ?? [200],
          expect_body: m.http.expect_body ?? null,
          expect_header: m.http.expect_header
            ? { name: m.http.expect_header.name ?? "", value: m.http.expect_header.value ?? null }
            : null,
        }
      : null,
    preset: m.preset ?? null,
    sni: m.sni ?? null,
    udp: m.udp
      ? { payload_hex: m.udp.payload_hex ?? null, require_reply: m.udp.require_reply ?? false }
      : null,
    grpc: m.grpc
      ? { service: m.grpc.service ?? "", expect_status: m.grpc.expect_status ?? [200] }
      : null,
  };
}

/** The "Check" column: what the monitor sends. */
export function describeCheck(m: Pick<MonitorForm, "type" | "http" | "udp" | "grpc">): string {
  if (m.type === "http" && m.http)
    return `${m.http.method} ${m.http.path} → ${m.http.expect_status.join(", ")}`;
  if (m.type === "grpc")
    return `HTTP/2 ${GRPC_HEALTH_PATH}${m.grpc?.service ? ` for ${m.grpc.service}` : ""}`;
  if (m.type === "udp")
    return m.udp?.require_reply ? "UDP probe, reply required" : "UDP probe, no reply required";
  return "TCP connect";
}

/** The standard gRPC health method the check calls (ADR 0227). */
const GRPC_HEALTH_PATH = "/grpc.health.v1.Health/Check";

function useMonitorList(tenant: string, changeSet?: string) {
  return useQuery({
    queryKey: ["t", tenant, "health-monitors", { changeSet }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/health-monitors", {
          params: { path: { t: tenant }, query: { limit: 200, change_set: changeSet } },
        }),
      ),
  });
}

function useEngines(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "engines"],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/tenants/{t}/engines", { params: { path: { t: tenant } } })),
  });
}

function TestBox({ tenant, monitor }: { tenant: string; monitor: Monitor }) {
  const pools = usePools(tenant);
  const engines = useEngines(tenant);
  const using = (pools.data?.items ?? []).filter((p) => p.health_monitor_id === monitor.id);
  const options = using.flatMap((p) => p.members.map((m) => ({ pool: p, member: m })));
  const [pick, setPick] = useState("");
  const [engine, setEngine] = useState("");
  const run = useMutation({
    mutationFn: async () => {
      const o = options.find((x) => x.member.id === pick)!;
      return unwrap(
        await api.POST("/api/v1/tenants/{t}/health-monitors/{monitor_id}/test", {
          params: { path: { t: tenant, monitor_id: monitor.id } },
          body: {
            pool_id: o.pool.id,
            member_id: o.member.id,
            ...(engine ? { engine_id: engine } : {}),
          },
        }),
      );
    },
  });
  return (
    <section className="space-y-2 rounded-lg border p-4">
      <h3 className="text-sm font-medium">{t("Test against one member")}</h3>
      <div className="grid grid-cols-2 gap-3">
        <select
          aria-label={t("Member to test")}
          className={INPUT}
          value={pick}
          onChange={(e) => setPick(e.target.value)}
        >
          <option value="">{t("Member to test")}</option>
          {options.map(({ pool, member }) => (
            <option key={member.id} value={member.id}>
              {member.name ?? member.address} · {hostPort(member.address, member.port)} ({pool.name}
              )
            </option>
          ))}
        </select>
        <select
          aria-label={t("From engine")}
          className={INPUT}
          value={engine}
          onChange={(e) => setEngine(e.target.value)}
        >
          <option value="">{t("Any engine")}</option>
          {engines.data?.map((e) => (
            <option key={e.id} value={e.id}>
              {e.name}
            </option>
          ))}
        </select>
      </div>
      <Button
        type="button"
        size="sm"
        disabled={!pick || run.isPending}
        onClick={() => run.mutate()}
      >
        {t("Run test")}
      </Button>
      {run.data && (
        <div
          role="status"
          className={cn(
            "rounded-md border p-3 text-sm",
            run.data.ok
              ? "border-status-healthy/40 bg-status-healthy/10"
              : "border-status-down/40 bg-status-down/10",
          )}
        >
          <p className="font-medium">
            {run.data.ok ? t("Passed") : t("Failed")} · {run.data.detail}
          </p>
          <p className="font-mono text-xs text-muted-foreground">
            {run.data.target} · {run.data.protocol} · {t("from {e}", { e: run.data.engine })} ·{" "}
            {run.data.latency_ms} ms
          </p>
        </div>
      )}
      {run.error && <ErrorState error={run.error} />}
      <p className="text-xs text-muted-foreground">
        {t(
          "Runs this monitor's saved settings once from the engine you pick. Nothing is saved and the live check is not changed.",
        )}
      </p>
    </section>
  );
}

function MonitorSheet({
  tenant,
  monitor,
  onClose,
}: {
  tenant: string;
  monitor: Monitor | "new";
  onClose: () => void;
}) {
  const save = useSaveOp(tenant, "health_monitor");
  const can = useCan(tenant);
  const write = can("write", "application");
  const pools = usePools(tenant);
  const base = useMemo(() => (monitor === "new" ? emptyMonitor() : toForm(monitor)), [monitor]);
  const [f, setF] = useState<MonitorForm>(base);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<unknown>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const set = (p: Partial<MonitorForm>) => setF({ ...f, ...p });
  const patch = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const k of Object.keys(f) as (keyof MonitorForm)[])
      if (JSON.stringify(base[k]) !== JSON.stringify(f[k])) out[k] = f[k];
    return out;
  }, [f, base]);
  const dirty = monitor === "new" ? f.name !== "" : Object.keys(patch).length > 0;
  const udp = f.type === "udp";
  const using =
    monitor === "new"
      ? []
      : (pools.data?.items ?? []).filter((p) => p.health_monitor_id === monitor.id);

  async function submit() {
    setProblem(null);
    setErrors({});
    try {
      const body = {
        ...f,
        http:
          f.type === "http"
            ? (f.http ?? { method: "GET", path: "/", host: null, expect_status: [200] })
            : null,
        udp: udp ? (f.udp ?? { payload_hex: null, require_reply: false }) : null,
        grpc:
          f.type === "grpc"
            ? (f.grpc ?? { service: "", expect_status: [200] })
            : null,
        preset: f.type === "tcp" ? (f.preset ?? null) : null,
        sni: f.type === "udp" ? null : f.sni?.trim() || null,
      };
      if (monitor === "new") await save.mutateAsync({ action: "create", body });
      else
        await save.mutateAsync({
          action: "update",
          id: monitor.id,
          version: monitor.version,
          body: patch,
        });
      onClose();
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        const m: Record<string, string> = {};
        for (const x of e.problem?.errors ?? []) m[x.path] = x.message;
        setErrors(m);
      } else setProblem(e);
    }
  }
  const num = (k: "interval_s" | "timeout_s" | "rise" | "fall", label: string) => (
    <label className="text-sm font-medium">
      {label}
      <input
        aria-label={label}
        type="number"
        className={INPUT}
        disabled={!write.allowed}
        value={f[k]}
        onChange={(e) => set({ [k]: Number(e.target.value) })}
      />
      {errors[k] && (
        <span role="alert" className="block text-xs font-normal text-status-down">
          {errors[k]}
        </span>
      )}
    </label>
  );

  return (
    <>
      <Sheet
        open
        onOpenChange={(o) => !o && onClose()}
        title={
          monitor === "new" ? t("New monitor") : t("Edit monitor {name}", { name: monitor.name })
        }
        badge={
          monitor !== "new" && monitor.draft_status !== "unchanged" ? (
            <DraftMarker status={monitor.draft_status} />
          ) : undefined
        }
        footer={
          <>
            {monitor !== "new" && (
              <Tip label={write.reason}>
                <Button
                  variant="outline"
                  disabled={!write.allowed}
                  onClick={() => setConfirmDelete(true)}
                >
                  {t("Delete")}
                </Button>
              </Tip>
            )}
            <Button variant="ghost" className="ml-auto" onClick={onClose}>
              {t("Cancel")}
            </Button>
            <Tip label={write.reason}>
              <Button
                disabled={!write.allowed || !dirty || save.isPending}
                onClick={() => void submit()}
              >
                {t("Save to draft")}
              </Button>
            </Tip>
          </>
        }
      >
        {problem != null && <ErrorState error={problem} />}
        <label className="block space-y-1.5 text-sm font-medium">
          {t("Name")}
          <input
            aria-label={t("Name")}
            className={INPUT}
            disabled={!write.allowed}
            value={f.name}
            onChange={(e) => set({ name: e.target.value })}
          />
          {errors.name && (
            <span role="alert" className="block text-xs font-normal text-status-down">
              {errors.name}
            </span>
          )}
        </label>
        <div className="space-y-1.5">
          <span id="monitor-type" className="text-sm font-medium">
            {t("Type")}
          </span>
          <div
            role="radiogroup"
            aria-labelledby="monitor-type"
            className="inline-flex rounded-md border"
          >
            {(["tcp", "http", "grpc", "udp"] as const).map((k) => (
              <button
                key={k}
                type="button"
                role="radio"
                aria-checked={f.type === k}
                disabled={!write.allowed || monitor !== "new"}
                onClick={() => set({ type: k })}
                className={cn(
                  "px-4 py-1.5 text-sm first:rounded-l-md last:rounded-r-md disabled:cursor-not-allowed",
                  f.type === k
                    ? "bg-accent font-medium"
                    : "text-muted-foreground hover:bg-accent/50",
                )}
              >
                {k.toUpperCase()}
              </button>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">
            {t(
              "TCP, HTTP and gRPC monitors check TCP and HTTP pools (gRPC only a pool that speaks HTTP/2); UDP monitors check UDP pools only.",
            )}
          </p>
        </div>
        <div className="grid grid-cols-4 gap-3">
          {num("interval_s", t("Interval"))}
          {num("timeout_s", t("Timeout"))}
          {!udp && num("rise", t("Rise"))}
          {num("fall", t("Fall"))}
        </div>
        {udp && (
          <p className="text-xs text-muted-foreground">
            {t("UDP checks have no rise count: a member comes back after its next passing check.")}
          </p>
        )}
        <label className="block space-y-1.5 text-sm font-medium">
          {t("Port")}
          <input
            aria-label={t("Port")}
            type="number"
            placeholder={t("Member's port")}
            className={cn(INPUT, "max-w-40")}
            disabled={!write.allowed}
            value={f.port ?? ""}
            onChange={(e) => set({ port: e.target.value ? Number(e.target.value) : null })}
          />
          <span className="block text-xs font-normal text-muted-foreground">
            {t("Leave empty to check each member on its own port.")}
          </span>
        </label>
        {f.type === "http" && (
          <fieldset className="space-y-3 rounded-lg border p-4">
            <legend className="px-1 text-sm font-medium">{t("HTTP request")}</legend>
            {(() => {
              const h = f.http ?? { method: "GET", path: "/", host: null, expect_status: [200] };
              const sh = (p: Partial<typeof h>) => set({ http: { ...h, ...p } });
              return (
                <>
                  <div className="grid grid-cols-[8rem_1fr] gap-3">
                    <label className="space-y-1.5 text-sm font-medium">
                      {t("Method")}
                      <select
                        aria-label={t("Method")}
                        className={INPUT}
                        disabled={!write.allowed}
                        value={h.method}
                        onChange={(e) => sh({ method: e.target.value })}
                      >
                        <option>GET</option>
                        <option>HEAD</option>
                        <option>OPTIONS</option>
                      </select>
                    </label>
                    <label className="space-y-1.5 text-sm font-medium">
                      {t("Path")}
                      <input
                        aria-label={t("Path")}
                        className={cn(INPUT, "font-mono")}
                        disabled={!write.allowed}
                        value={h.path}
                        onChange={(e) => sh({ path: e.target.value })}
                      />
                    </label>
                  </div>
                  <label className="block space-y-1.5 text-sm font-medium">
                    {t("Host header")}
                    <input
                      aria-label={t("Host header")}
                      placeholder={t("Optional, e.g. api.example.com")}
                      className={cn(INPUT, "font-mono")}
                      disabled={!write.allowed}
                      value={h.host ?? ""}
                      onChange={(e) => sh({ host: e.target.value || null })}
                    />
                  </label>
                  <ChipInput
                    label={t("Expected status codes")}
                    value={h.expect_status.map(String)}
                    onChange={(v) => sh({ expect_status: v.map(Number) })}
                    freeText={{
                      validate: (x) =>
                        /^[1-5]\d\d$/.test(x.trim()) ? null : t("Use a status code, 100 to 599."),
                    }}
                    placeholder={t("Add code")}
                    disabled={!write.allowed}
                  />
                  <div className="grid gap-3 sm:grid-cols-[11rem_1fr_1fr]">
                    <label className="space-y-1.5 text-sm font-medium">
                      {t("Healthy when")}
                      <select
                        aria-label={t("Healthy when")}
                        className={INPUT}
                        disabled={!write.allowed}
                        value={h.expect_body != null ? "body" : h.expect_header ? "header" : "none"}
                        onChange={(e) =>
                          sh({
                            expect_body: e.target.value === "body" ? "" : null,
                            expect_header:
                              e.target.value === "header" ? { name: "", value: null } : null,
                          })
                        }
                      >
                        <option value="none">{t("Status is one of the codes above")}</option>
                        <option value="body">{t("Body contains")}</option>
                        <option value="header">{t("Header has")}</option>
                      </select>
                    </label>
                    {h.expect_body != null && (
                      <label className="space-y-1.5 text-sm font-medium sm:col-span-2">
                        {t("Text the body must contain")}
                        <input
                          aria-label={t("Expected body")}
                          className={cn(INPUT, "font-mono")}
                          disabled={!write.allowed}
                          value={h.expect_body}
                          onChange={(e) => sh({ expect_body: e.target.value })}
                        />
                      </label>
                    )}
                    {h.expect_header && (
                      <>
                        <label className="space-y-1.5 text-sm font-medium">
                          {t("Header name")}
                          <input
                            aria-label={t("Expected header name")}
                            className={cn(INPUT, "font-mono")}
                            disabled={!write.allowed}
                            value={h.expect_header.name}
                            onChange={(e) =>
                              sh({ expect_header: { ...h.expect_header!, name: e.target.value } })
                            }
                          />
                        </label>
                        <label className="space-y-1.5 text-sm font-medium">
                          {t("Value (optional)")}
                          <input
                            aria-label={t("Expected header value")}
                            className={cn(INPUT, "font-mono")}
                            disabled={!write.allowed}
                            value={h.expect_header.value ?? ""}
                            onChange={(e) =>
                              sh({
                                expect_header: {
                                  ...h.expect_header!,
                                  value: e.target.value || null,
                                },
                              })
                            }
                          />
                        </label>
                      </>
                    )}
                  </div>
                  <p className="text-xs text-muted-foreground">
                    {t("One extra matcher per check: the body or a response header, not both.")}
                  </p>
                </>
              );
            })()}
          </fieldset>
        )}
        {f.type === "grpc" && (
          <fieldset className="space-y-3 rounded-lg border p-4">
            <legend className="px-1 text-sm font-medium">{t("gRPC health probe")}</legend>
            <label className="block space-y-1.5 text-sm font-medium">
              {t("Service name")}
              <input
                aria-label={t("Service name")}
                placeholder="acme.echo.Health"
                className={cn(INPUT, "font-mono")}
                disabled={!write.allowed}
                value={f.grpc?.service ?? ""}
                onChange={(e) =>
                  set({
                    grpc: {
                      service: e.target.value,
                      expect_status: f.grpc?.expect_status ?? [200],
                    },
                  })
                }
              />
              <span className="block text-xs font-normal text-muted-foreground">
                {t("Letters, digits, dots, dashes and underscores; empty means the whole server.")}
              </span>
            </label>
            <ChipInput
              label={t("Expected status codes")}
              value={(f.grpc?.expect_status ?? [200]).map(String)}
              onChange={(v) =>
                set({ grpc: { service: f.grpc?.service ?? "", expect_status: v.map(Number) } })
              }
              freeText={{
                validate: (x) =>
                  /^[1-5]\d\d$/.test(x.trim()) ? null : t("Use a status code, 100 to 599."),
              }}
              placeholder={t("Add code")}
              disabled={!write.allowed}
            />
            <p className="text-xs text-muted-foreground">
              {t(
                "Runs a POST to the standard gRPC health method over HTTP/2. The pool must use backend protocol h2 or h2c. HAProxy cannot read grpc-status, so a server that answers NOT_SERVING still passes.",
              )}
            </p>
          </fieldset>
        )}
        {f.type === "tcp" && (
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Protocol check")}
            <select
              aria-label={t("Protocol check")}
              className={INPUT}
              disabled={!write.allowed}
              value={f.preset ?? ""}
              onChange={(e) => set({ preset: (e.target.value || null) as MonitorForm["preset"] })}
            >
              <option value="">{t("None (just connect)")}</option>
              <option value="redis">Redis</option>
              <option value="smtp">SMTP</option>
              <option value="ldap">LDAP</option>
              <option value="mysql">MySQL</option>
            </select>
            <span className="block text-xs font-normal text-muted-foreground">
              {t(
                "HAProxy's built-in check for the protocol; the member must answer it, not only accept the connection.",
              )}
            </span>
          </label>
        )}
        {f.type !== "udp" && (
          <label className="block space-y-1.5 text-sm font-medium">
            {t("TLS server name (SNI)")}
            <input
              aria-label={t("TLS server name (SNI)")}
              placeholder="app.example.org"
              className={INPUT}
              disabled={!write.allowed}
              value={f.sni ?? ""}
              onChange={(e) => set({ sni: e.target.value || null })}
            />
            <span className="block text-xs font-normal text-muted-foreground">
              {t(
                "Sent when a pool checks its members over TLS. Empty uses the host of a route to the pool.",
              )}
            </span>
          </label>
        )}
        {udp && (
          <fieldset className="space-y-3 rounded-lg border p-4">
            <legend className="px-1 text-sm font-medium">{t("UDP probe")}</legend>
            <input
              aria-label={t("Probe payload (hex)")}
              placeholder={t("Probe payload (hex)")}
              className={cn(INPUT, "font-mono")}
              disabled={!write.allowed}
              value={f.udp?.payload_hex ?? ""}
              onChange={(e) =>
                set({
                  udp: {
                    payload_hex: e.target.value || null,
                    require_reply: f.udp?.require_reply ?? false,
                  },
                })
              }
            />
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                disabled={!write.allowed}
                checked={f.udp?.require_reply ?? false}
                onChange={(e) =>
                  set({
                    udp: {
                      payload_hex: f.udp?.payload_hex ?? null,
                      require_reply: e.target.checked,
                    },
                  })
                }
              />
              {t("Require a reply")}
            </label>
          </fieldset>
        )}
        {monitor !== "new" && <TestBox tenant={tenant} monitor={monitor} />}
        {monitor !== "new" && using.length > 0 && (
          <p className="text-xs text-muted-foreground">
            {t(
              "Used by {n} pools. A change here re-renders all of them and shows in the review's Config tab.",
              { n: using.length },
            )}
          </p>
        )}
      </Sheet>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete monitor {name}?", { name: monitor === "new" ? "" : monitor.name })}
        confirmLabel={t("Add delete to draft")}
        busy={save.isPending}
        onConfirm={async () => {
          if (monitor === "new") return;
          try {
            await save.mutateAsync({ action: "delete", id: monitor.id, version: monitor.version });
            onClose();
          } catch (e) {
            setProblem(e);
          } finally {
            setConfirmDelete(false);
          }
        }}
      >
        <p>
          {using.length > 0
            ? t(
                "{n} pools use this monitor; the validator blocks the apply until they use another.",
                { n: using.length },
              )
            : t("No pool uses this monitor.")}
        </p>
      </ConfirmDialog>
    </>
  );
}

/** Health monitors: what each checks, timing, and the pools using it. */
export function Monitors({ tenant }: { tenant: string }) {
  const { draft } = useDraft(tenant);
  const list = useMonitorList(tenant, draft?.id);
  const pools = usePools(tenant, draft?.id);
  const can = useCan(tenant);
  const write = can("write", "application");
  const [editing, setEditing] = useState<Monitor | "new" | null>(null);
  const usedBy = new Map<string, string[]>();
  for (const p of pools.data?.items ?? [])
    if (p.health_monitor_id)
      usedBy.set(p.health_monitor_id, [...(usedBy.get(p.health_monitor_id) ?? []), p.name]);
  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("Health monitors")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("{n} monitors", { n: list.data?.items.length ?? 0 })}
          </p>
        </div>
        <Tip label={write.reason}>
          <Button disabled={!write.allowed} onClick={() => setEditing("new")}>
            <Plus /> {t("New monitor")}
          </Button>
        </Tip>
      </div>
      <div className="overflow-x-auto rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={6} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : list.data.items.length === 0 ? (
          <EmptyState
            title={t("No health monitors yet")}
            body={t("A monitor decides how the engines tell whether a pool member is up.")}
            action={
              write.allowed ? (
                <Button onClick={() => setEditing("new")}>{t("Create a monitor")}</Button>
              ) : undefined
            }
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {[
                  "Name",
                  "Type",
                  "Check",
                  "Interval · timeout",
                  "Rise · fall",
                  "Pools using it",
                ].map((h) => (
                  <th key={h} className="px-4 py-2.5 font-medium">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {list.data.items.map((m) => (
                <tr key={m.id}>
                  <td className="px-4 py-3">
                    <span className="flex items-center gap-2">
                      {m.draft_status !== "unchanged" && <DraftMarker status={m.draft_status} />}
                      <button
                        type="button"
                        className={cn(
                          "font-medium text-primary hover:underline",
                          m.draft_status === "deleted" && "line-through",
                        )}
                        onClick={() => setEditing(m)}
                      >
                        {m.name}
                      </button>
                    </span>
                  </td>
                  <td className="px-4 py-3 uppercase">{m.type}</td>
                  <td className="px-4 py-3 font-mono text-xs">{t(describeCheck(toForm(m)))}</td>
                  <td className="px-4 py-3">
                    {m.interval_s} s · {m.timeout_s} s
                  </td>
                  <td className="px-4 py-3">
                    {m.type === "udp" ? "—" : m.rise} · {m.fall}
                  </td>
                  <td className="px-4 py-3">{usedBy.get(m.id)?.join(", ") ?? t("none")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {editing && (
        <MonitorSheet
          key={
            editing === "new" ? "new" : `${editing.id}-${editing.version}-${editing.draft_status}`
          }
          tenant={tenant}
          monitor={editing}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}
