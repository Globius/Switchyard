import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { Signpost } from "lucide-react";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import { useCan } from "@/auth/session";
import { expiryStatus, StatusPill } from "@/components/sy/StatusPill";
import { ChipInput } from "@/components/sy/ChipInput";
import { ErrorState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useDraft } from "@/features/shell/draft";
import { useEnsureDraft } from "@/features/shell/draft-write";
import { usePools } from "@/features/vs/detail-queries";
import { useCertificates, useSeGroups, useVirtualServices } from "@/features/vs/queries";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import {
  buildOps,
  downAfterSeconds,
  initialState,
  stepTitles,
  validateStep,
  withProtocol,
  type Algorithm,
  type Protocol,
  type WizardState,
} from "./model";

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

type NumField = "interval_s" | "timeout_s" | "rise" | "fall";

/** UDP monitors have no rise count. */
function monitorFields(udp: boolean): NumField[] {
  return udp ? ["interval_s", "timeout_s", "fall"] : ["interval_s", "timeout_s", "rise", "fall"];
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

function useMonitors(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "health-monitors"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/health-monitors", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
}

function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm font-medium">{label}</label>
      {children}
      {hint && !error && <p className="text-xs text-muted-foreground">{hint}</p>}
      {error && (
        <p role="alert" className="text-xs text-status-down">
          {error}
        </p>
      )}
    </div>
  );
}

/** Full-screen wizard: basics, VIP, ports and TLS, routes and pools, health check, review. */
export function Wizard({ tenant }: { tenant: string }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const can = useCan(tenant);
  const create = can("write", "application");
  const { draft } = useDraft(tenant);
  const ensure = useEnsureDraft(tenant);
  const groups = useSeGroups(tenant);
  const vipPools = useVipPools(tenant);
  const certs = useCertificates(tenant, draft?.id);
  const pools = usePools(tenant);
  const monitors = useMonitors(tenant);
  const existing = useVirtualServices(tenant, {});

  const [s, setS] = useState<WizardState>(initialState);
  const [step, setStep] = useState(1);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);
  const [suggested, setSuggested] = useState<string | null>(null);

  const titles = stepTitles(s.protocol);
  const names = new Set(existing.data?.pages.flatMap((p) => p.items.map((v) => v.name)) ?? []);
  const set = (patch: Partial<WizardState>) => setS((cur) => ({ ...cur, ...patch }));
  const udp = s.protocol === "udp";
  const http = s.protocol === "http";
  const monitorOptions = (monitors.data?.items ?? []).filter((m) =>
    udp ? m.type === "udp" : m.type !== "udp",
  );

  /** Ask the pool for its next free address. It is a suggestion: nothing is reserved. */
  async function suggest(poolId: string): Promise<boolean> {
    try {
      const r = unwrap(
        await api.POST("/api/v1/tenants/{t}/vip-pools/{pool_id}/next-free", {
          params: { path: { t: tenant, pool_id: poolId } },
        }),
      );
      set({ vip: r.address });
      setSuggested(r.address);
      setProblem(null);
      return true;
    } catch (err) {
      setProblem(err);
      return false;
    }
  }

  async function next() {
    const e = validateStep(step, s, names);
    setErrors(e);
    if (Object.keys(e).length > 0) return;
    if (step === 2 && s.vipMode === "pool" && !s.vip && !(await suggest(s.vip_pool_id))) return;
    setProblem(null);
    setStep(step + 1);
  }

  async function finish() {
    setSaving(true);
    setProblem(null);
    try {
      const built = buildOps(s);
      const cs = await ensure.mutateAsync();
      unwrap(
        await api.POST("/api/v1/tenants/{t}/change-sets/{cs}/ops", {
          params: { path: { t: tenant, cs } },
          body: { ops: built.ops },
        }),
      );
      await qc.invalidateQueries({ queryKey: ["t", tenant] });
      void navigate({ to: "/t/$tenant/virtual-services/$id", params: { tenant, id: built.vsId } });
    } catch (err) {
      setProblem(err);
    } finally {
      setSaving(false);
    }
  }

  const built = step === 6 ? buildOps(s, () => "…") : null;
  const opCount = built?.ops.length ?? 0;
  const groupName = groups.data?.items.find((g) => g.id === s.se_group_id)?.name;

  return (
    <div className="min-h-screen bg-background">
      <header className="flex h-14 items-center gap-3 border-b bg-card px-5">
        <span className="flex items-center gap-2 font-semibold">
          <span className="grid size-7 place-items-center rounded-md bg-primary text-primary-foreground">
            <Signpost className="size-4" aria-hidden />
          </span>
          Switchyard
        </span>
        <span aria-hidden className="h-6 w-px bg-border" />
        <span className="text-sm text-muted-foreground">
          {t("Tenant")} <span className="font-medium text-foreground">{tenant}</span>
        </span>
        <h1 className="sr-only">{t("New virtual service")}</h1>
        <Button variant="ghost" className="ml-auto" asChild>
          <Link to="/t/$tenant/virtual-services" params={{ tenant }}>
            {t("Cancel")}
          </Link>
        </Button>
      </header>
      <div className="mx-auto flex max-w-5xl gap-10 p-8">
        <ol aria-label={t("Steps")} className="w-48 shrink-0 space-y-1 text-sm">
          {titles.map((title, i) => (
            <li
              key={title}
              aria-current={step === i + 1 ? "step" : undefined}
              className={cn(
                "flex items-center gap-2 rounded-md px-3 py-2",
                step === i + 1 && "bg-accent font-medium",
                step > i + 1 && "text-muted-foreground",
              )}
            >
              <span
                className={cn(
                  "grid size-6 place-items-center rounded-full border text-xs",
                  step === i + 1 && "border-primary bg-primary text-primary-foreground",
                  step > i + 1 && "border-transparent bg-status-healthy/15 text-status-healthy",
                )}
              >
                {step > i + 1 ? "✓" : i + 1}
              </span>
              {t(title)}
            </li>
          ))}
        </ol>

        <main className="min-w-0 flex-1 space-y-6">
          {step === 1 && (
            <section className="space-y-5">
              <h1 className="text-xl font-semibold">{t("Basics")}</h1>
              <Field
                label={t("Name")}
                hint={t("Lower case, digits and hyphens, unique in {tenant}.", { tenant })}
                error={errors.name}
              >
                <input
                  aria-label={t("Name")}
                  className={INPUT}
                  value={s.name}
                  onChange={(e) => set({ name: e.target.value })}
                />
              </Field>
              <fieldset className="space-y-2">
                <legend className="text-sm font-medium">{t("Protocol")}</legend>
                {(
                  [
                    [
                      "http",
                      "HTTP",
                      "Layer 7. Host and path routes, TLS, HTTP/2, redirects, policies.",
                    ],
                    ["tcp", "TCP", "Layer 4 stream. Optional TLS termination and PROXY protocol."],
                    ["udp", "UDP", "Layer 4 datagrams through IPVS. Ports and one pool only."],
                  ] as [Protocol, string, string][]
                ).map(([p, label, help]) => (
                  <label
                    key={p}
                    className={cn(
                      "flex cursor-pointer gap-3 rounded-lg border bg-card p-3",
                      s.protocol === p && "border-primary",
                    )}
                  >
                    <input
                      type="radio"
                      name="protocol"
                      aria-label={label}
                      checked={s.protocol === p}
                      onChange={() => setS(withProtocol(s, p))}
                    />
                    <span>
                      <span className="font-medium">{label}</span>
                      <span className="block text-sm text-muted-foreground">{t(help)}</span>
                    </span>
                  </label>
                ))}
              </fieldset>
              <Field label={t("SE group")} error={errors.se_group_id}>
                <select
                  aria-label={t("SE group")}
                  className={INPUT}
                  value={s.se_group_id}
                  onChange={(e) => set({ se_group_id: e.target.value })}
                >
                  <option value="">{t("Choose…")}</option>
                  {groups.data?.items.map((g) => (
                    <option key={g.id} value={g.id}>
                      {g.name} · {g.ha_mode.toUpperCase()}
                    </option>
                  ))}
                </select>
              </Field>
            </section>
          )}

          {step === 2 && (
            <section className="space-y-5">
              <h1 className="text-xl font-semibold">{t("VIP")}</h1>
              <p className="text-sm text-muted-foreground">
                {t("The address clients connect to.")}
              </p>
              <label className="flex items-start gap-3 rounded-lg border bg-card p-3">
                <input
                  type="radio"
                  name="vipMode"
                  aria-label={t("Allocate from a VIP pool")}
                  checked={s.vipMode === "pool"}
                  onChange={() => set({ vipMode: "pool", vip: "" })}
                />
                <span className="flex-1 space-y-2">
                  <span className="font-medium">{t("Allocate from a VIP pool")}</span>
                  <select
                    aria-label={t("VIP pool")}
                    className={INPUT}
                    value={s.vip_pool_id}
                    onChange={(e) => {
                      set({ vip_pool_id: e.target.value, vip: "" });
                      setSuggested(null);
                      if (e.target.value) void suggest(e.target.value);
                    }}
                  >
                    <option value="">{t("Choose…")}</option>
                    {vipPools.data?.items.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                  {errors.vip_pool_id && (
                    <span role="alert" className="block text-xs text-status-down">
                      {errors.vip_pool_id}
                    </span>
                  )}
                  {vipPools.data?.items.length === 0 && (
                    <span className="block text-xs text-muted-foreground">
                      {t("No VIP pools yet. Create one under Networks and VIP pools.")}
                    </span>
                  )}
                  {s.vipMode === "pool" && s.vip && (
                    <span className="flex flex-wrap items-center gap-2">
                      <input
                        readOnly
                        aria-label={t("Suggested address")}
                        className={cn(INPUT, "w-44 font-mono")}
                        value={s.vip}
                      />
                      <StatusPill status="neutral">{t("Suggested, not reserved")}</StatusPill>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => void suggest(s.vip_pool_id)}
                      >
                        {t("Suggest again")}
                      </Button>
                    </span>
                  )}
                  <span className="block text-xs text-muted-foreground">
                    {t(
                      "Nothing is reserved. If another draft takes the same address, Review blocks the change set that is applied second; your other fields are kept.",
                    )}
                  </span>
                </span>
              </label>
              <label className="flex items-start gap-3 rounded-lg border bg-card p-3">
                <input
                  type="radio"
                  name="vipMode"
                  aria-label={t("Fixed address")}
                  checked={s.vipMode === "fixed"}
                  onChange={() => set({ vipMode: "fixed", vip: "" })}
                />
                <span className="flex-1 space-y-2">
                  <span className="font-medium">{t("Fixed address")}</span>
                  <input
                    aria-label={t("VIP address")}
                    className={cn(INPUT, "font-mono")}
                    value={s.vipMode === "fixed" ? s.vip : ""}
                    disabled={s.vipMode !== "fixed"}
                    onChange={(e) => set({ vip: e.target.value })}
                  />
                  {errors.vip && (
                    <span role="alert" className="block text-xs text-status-down">
                      {errors.vip}
                    </span>
                  )}
                  <span className="block text-xs text-muted-foreground">
                    {t("Must sit inside a VIP network of the SE group and not be in use.")}
                  </span>
                </span>
              </label>
            </section>
          )}

          {step === 3 && (
            <section className="space-y-5">
              <h1 className="text-xl font-semibold">{t("Ports and TLS")}</h1>
              {errors.ports && (
                <p role="alert" className="text-xs text-status-down">
                  {errors.ports}
                </p>
              )}
              {s.ports.map((p, i) => (
                <div
                  key={i}
                  className="flex flex-wrap items-center gap-4 rounded-lg border bg-card p-3 text-sm"
                >
                  <div>
                    <input
                      aria-label={t("Port {n}", { n: i + 1 })}
                      type="number"
                      className={cn(INPUT, "w-28 font-mono")}
                      value={p.port}
                      onChange={(e) =>
                        set({
                          ports: s.ports.map((x, j) =>
                            j === i ? { ...x, port: Number(e.target.value) } : x,
                          ),
                        })
                      }
                    />
                    {errors[`ports.${i}.port`] && (
                      <span role="alert" className="block text-xs text-status-down">
                        {errors[`ports.${i}.port`]}
                      </span>
                    )}
                  </div>
                  {!udp && (
                    <label className="flex items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={p.tls}
                        onChange={(e) =>
                          set({
                            ports: s.ports.map((x, j) =>
                              j === i ? { ...x, tls: e.target.checked } : x,
                            ),
                          })
                        }
                      />
                      {s.protocol === "tcp" ? t("TLS termination") : "TLS"}
                    </label>
                  )}
                  {http && (
                    <label className="flex items-center gap-1.5">
                      <input
                        type="checkbox"
                        disabled={!p.tls}
                        checked={p.http2}
                        onChange={(e) =>
                          set({
                            ports: s.ports.map((x, j) =>
                              j === i ? { ...x, http2: e.target.checked } : x,
                            ),
                          })
                        }
                      />
                      HTTP/2
                    </label>
                  )}
                  {!udp && (
                    <label className="flex items-center gap-1.5">
                      <input
                        type="checkbox"
                        checked={p.proxy_protocol}
                        onChange={(e) =>
                          set({
                            ports: s.ports.map((x, j) =>
                              j === i ? { ...x, proxy_protocol: e.target.checked } : x,
                            ),
                          })
                        }
                      />
                      {s.protocol === "tcp" ? t("PROXY protocol to backends") : "PROXY"}
                    </label>
                  )}
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    disabled={s.ports.length === 1}
                    onClick={() => set({ ports: s.ports.filter((_, j) => j !== i) })}
                  >
                    {t("Remove")}
                  </Button>
                </div>
              ))}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() =>
                  set({
                    ports: [
                      ...s.ports,
                      {
                        port: 80,
                        tls: false,
                        http2: false,
                        proxy_protocol: false,
                        https_redirect_port: null,
                      },
                    ],
                  })
                }
              >
                {t("Add port")}
              </Button>
              {udp ? (
                <p className="text-xs text-muted-foreground">
                  {t(
                    "No TLS, HTTP/2 or PROXY protocol on UDP. Each port forwards to the same port on the pool members.",
                  )}
                </p>
              ) : (
                <Field
                  label={t("Certificates for TLS ports")}
                  error={errors.certificate_ids}
                  hint={
                    s.protocol === "tcp"
                      ? t(
                          "Leave TLS off to pass encrypted traffic straight through to the backends.",
                        )
                      : undefined
                  }
                >
                  <ChipInput
                    label={t("Certificates")}
                    value={s.certificate_ids}
                    onChange={(v) => set({ certificate_ids: v })}
                    options={(certs.data?.items ?? []).map((c) => ({
                      value: c.id,
                      label: c.name,
                      hint: expiryStatus(c.not_after).label,
                    }))}
                    emptyMessage={(q) => t("No certificate named “{q}”.", { q })}
                    placeholder={t("Search certificates")}
                  />
                </Field>
              )}
            </section>
          )}

          {step === 4 && (
            <section className="space-y-5">
              <h1 className="text-xl font-semibold">{http ? t("Routes and pools") : t("Pool")}</h1>
              {http && (
                <div className="space-y-3">
                  <h2 className="text-sm font-semibold">{t("Routes (first match wins)")}</h2>
                  {s.routes.map((r, i) => {
                    const upd = (patch: Partial<typeof r>) =>
                      set({ routes: s.routes.map((x, j) => (j === i ? { ...x, ...patch } : x)) });
                    return (
                      <div
                        key={i}
                        className="grid gap-2 rounded-lg border bg-card p-3 text-sm md:grid-cols-5"
                      >
                        <input
                          aria-label={t("Route {n} host", { n: i + 1 })}
                          placeholder={t("host")}
                          className={INPUT}
                          value={r.host}
                          onChange={(e) => upd({ host: e.target.value })}
                        />
                        <select
                          aria-label={t("Route {n} match", { n: i + 1 })}
                          className={INPUT}
                          value={r.kind}
                          onChange={(e) => upd({ kind: e.target.value as "prefix" | "exact" })}
                        >
                          <option value="prefix">{t("Prefix")}</option>
                          <option value="exact">{t("Exact")}</option>
                        </select>
                        <input
                          aria-label={t("Route {n} path", { n: i + 1 })}
                          placeholder="/"
                          className={cn(INPUT, "font-mono")}
                          value={r.path}
                          onChange={(e) => upd({ path: e.target.value })}
                        />
                        <select
                          aria-label={t("Route {n} action", { n: i + 1 })}
                          className={INPUT}
                          value={r.action}
                          onChange={(e) => upd({ action: e.target.value as "pool" | "redirect" })}
                        >
                          <option value="pool">{t("Pool")}</option>
                          <option value="redirect">{t("Redirect")}</option>
                        </select>
                        {r.action === "redirect" ? (
                          <input
                            aria-label={t("Route {n} redirect location", { n: i + 1 })}
                            placeholder="https://…"
                            className={cn(INPUT, "font-mono")}
                            value={r.redirect_location}
                            onChange={(e) => upd({ redirect_location: e.target.value })}
                          />
                        ) : (
                          <span className="self-center text-muted-foreground">
                            {t("goes to the pool below")}
                          </span>
                        )}
                        {["", ".path", ".redirect"].map(
                          (k) =>
                            errors[`routes.${i}${k}`] && (
                              <span
                                key={k}
                                role="alert"
                                className="text-xs text-status-down md:col-span-5"
                              >
                                {errors[`routes.${i}${k}`]}
                              </span>
                            ),
                        )}
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => set({ routes: s.routes.filter((_, j) => j !== i) })}
                        >
                          {t("Remove")}
                        </Button>
                      </div>
                    );
                  })}
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      set({
                        routes: [
                          ...s.routes,
                          {
                            host: "",
                            kind: "prefix",
                            path: "",
                            action: "pool",
                            redirect_location: "",
                            redirect_code: 301,
                          },
                        ],
                      })
                    }
                  >
                    {t("Add route")}
                  </Button>
                </div>
              )}
              <Field label={http ? t("Default pool") : t("Pool")} error={errors.pool_choice}>
                <select
                  aria-label={t("Pool choice")}
                  className={INPUT}
                  value={s.pool_choice}
                  onChange={(e) => set({ pool_choice: e.target.value })}
                >
                  <option value="new">{t("New pool in this wizard")}</option>
                  {pools.data?.items.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </Field>
              {s.pool_choice === "new" && (
                <div className="space-y-4 rounded-lg border bg-card p-4">
                  <p className="text-xs text-muted-foreground">
                    {t("The new pool is added to your draft as a separate op.")}
                  </p>
                  <Field label={t("Pool name")} error={errors.new_pool_name}>
                    <input
                      aria-label={t("Pool name")}
                      className={INPUT}
                      value={s.new_pool_name}
                      onChange={(e) => set({ new_pool_name: e.target.value })}
                    />
                  </Field>
                  <Field
                    label={t("Algorithm")}
                    hint={
                      udp
                        ? t(
                            "Random isn't available on UDP. Use Source hash for stateful protocols such as WireGuard, so a failover keeps each client on the same backend.",
                          )
                        : undefined
                    }
                  >
                    <select
                      aria-label={t("Algorithm")}
                      className={INPUT}
                      value={s.algorithm}
                      onChange={(e) => set({ algorithm: e.target.value as Algorithm })}
                    >
                      <option value="roundrobin">{t("Round robin")}</option>
                      <option value="leastconn">{t("Least connections")}</option>
                      <option value="source">{t("Source hash")}</option>
                      {!udp && <option value="random">{t("Random")}</option>}
                    </select>
                  </Field>
                  <div className="space-y-2">
                    <div className="text-sm font-medium">{t("Members")}</div>
                    {errors.members && (
                      <p role="alert" className="text-xs text-status-down">
                        {errors.members}
                      </p>
                    )}
                    {s.members.map((m, i) => (
                      <div key={i} className="flex flex-wrap items-start gap-2">
                        <div>
                          <input
                            aria-label={t("Member {n} address", { n: i + 1 })}
                            placeholder={t("address")}
                            className={cn(INPUT, "w-44 font-mono")}
                            value={m.address}
                            onChange={(e) =>
                              set({
                                members: s.members.map((x, j) =>
                                  j === i ? { ...x, address: e.target.value } : x,
                                ),
                              })
                            }
                          />
                          {errors[`members.${i}.address`] && (
                            <span role="alert" className="block text-xs text-status-down">
                              {errors[`members.${i}.address`]}
                            </span>
                          )}
                        </div>
                        <div>
                          <input
                            aria-label={t("Member {n} port", { n: i + 1 })}
                            placeholder={t("port")}
                            className={cn(INPUT, "w-24 font-mono")}
                            value={m.port}
                            onChange={(e) =>
                              set({
                                members: s.members.map((x, j) =>
                                  j === i ? { ...x, port: e.target.value } : x,
                                ),
                              })
                            }
                          />
                          {errors[`members.${i}.port`] && (
                            <span role="alert" className="block text-xs text-status-down">
                              {errors[`members.${i}.port`]}
                            </span>
                          )}
                        </div>
                        <input
                          aria-label={t("Member {n} weight", { n: i + 1 })}
                          type="number"
                          className={cn(INPUT, "w-20")}
                          value={m.weight}
                          onChange={(e) =>
                            set({
                              members: s.members.map((x, j) =>
                                j === i ? { ...x, weight: Number(e.target.value) } : x,
                              ),
                            })
                          }
                        />
                        <Button
                          type="button"
                          variant="ghost"
                          size="sm"
                          onClick={() => set({ members: s.members.filter((_, j) => j !== i) })}
                        >
                          {t("Remove")}
                        </Button>
                      </div>
                    ))}
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() =>
                        set({ members: [...s.members, { address: "", port: "", weight: 1 }] })
                      }
                    >
                      {t("Add member")}
                    </Button>
                  </div>
                </div>
              )}
            </section>
          )}

          {step === 5 && (
            <section className="space-y-5">
              <h1 className="text-xl font-semibold">{t("Health check")}</h1>
              {s.pool_choice !== "new" ? (
                <p className="text-sm text-muted-foreground">
                  {t("The pool you chose keeps its own health check.")}
                </p>
              ) : (
                <>
                  <Field label={t("Monitor")}>
                    <select
                      aria-label={t("Monitor choice")}
                      className={INPUT}
                      value={s.monitor_choice}
                      onChange={(e) => set({ monitor_choice: e.target.value })}
                    >
                      <option value="new">
                        {udp ? t("Create a UDP monitor") : t("Create a monitor")}
                      </option>
                      <option value="">{t("No health check")}</option>
                      {monitorOptions.map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.name} · {m.type.toUpperCase()} · {m.interval_s} s
                        </option>
                      ))}
                    </select>
                  </Field>
                  {udp && (
                    <p className="text-xs text-muted-foreground">
                      {t("Only UDP monitors can check a UDP pool.")}
                    </p>
                  )}
                  {(() => {
                    const m = monitorOptions.find((x) => x.id === s.monitor_choice);
                    return m ? (
                      <p className="text-xs text-muted-foreground">
                        {m.type === "udp"
                          ? t("Checks every {i} s; down after {f} failed checks.", {
                              i: m.interval_s,
                              f: m.fall,
                            })
                          : t("Checks every {i} s. Up after {r} passing, down after {f} failing.", {
                              i: m.interval_s,
                              r: m.rise,
                              f: m.fall,
                            })}
                      </p>
                    ) : null;
                  })()}
                  {s.monitor_choice === "new" && (
                    <div className="grid gap-4 rounded-lg border bg-card p-4 md:grid-cols-2">
                      <Field label={t("Name")} error={errors.monitor_name}>
                        <input
                          aria-label={t("Monitor name")}
                          className={INPUT}
                          value={s.monitor.name}
                          onChange={(e) => set({ monitor: { ...s.monitor, name: e.target.value } })}
                        />
                      </Field>
                      {!udp && (
                        <Field label={t("Type")}>
                          <select
                            aria-label={t("Monitor type")}
                            className={INPUT}
                            value={s.monitor.type}
                            onChange={(e) =>
                              set({
                                monitor: { ...s.monitor, type: e.target.value as "tcp" | "http" },
                              })
                            }
                          >
                            <option value="tcp">TCP</option>
                            <option value="http">HTTP</option>
                          </select>
                        </Field>
                      )}
                      {s.monitor.type === "http" && (
                        <>
                          <Field label={t("Path")}>
                            <input
                              aria-label={t("Monitor path")}
                              className={cn(INPUT, "font-mono")}
                              value={s.monitor.path}
                              onChange={(e) =>
                                set({ monitor: { ...s.monitor, path: e.target.value } })
                              }
                            />
                          </Field>
                          <Field label={t("Expected status")}>
                            <input
                              aria-label={t("Expected status")}
                              className={cn(INPUT, "font-mono")}
                              value={s.monitor.expect_status}
                              onChange={(e) =>
                                set({ monitor: { ...s.monitor, expect_status: e.target.value } })
                              }
                            />
                          </Field>
                        </>
                      )}
                      {udp && (
                        <>
                          <Field
                            label={t("Probe payload (hex)")}
                            error={errors.monitor_payload}
                            hint={t("Sent to each member. Lower-case hex.")}
                          >
                            <input
                              aria-label={t("Probe payload")}
                              className={cn(INPUT, "font-mono")}
                              value={s.monitor.payload_hex}
                              onChange={(e) =>
                                set({ monitor: { ...s.monitor, payload_hex: e.target.value } })
                              }
                            />
                          </Field>
                          <label className="flex items-center gap-2 text-sm">
                            <input
                              type="checkbox"
                              checked={s.monitor.require_reply}
                              onChange={(e) =>
                                set({ monitor: { ...s.monitor, require_reply: e.target.checked } })
                              }
                            />
                            {t("Require a reply")}
                          </label>
                        </>
                      )}
                      {monitorFields(udp).map((k) => (
                        <Field
                          key={k}
                          label={
                            {
                              interval_s: t("Interval (s)"),
                              timeout_s: t("Timeout (s)"),
                              rise: t("Rise"),
                              fall: t("Fall"),
                            }[k]
                          }
                          error={k === "timeout_s" ? errors.monitor_timeout : undefined}
                        >
                          <input
                            aria-label={
                              {
                                interval_s: t("Interval"),
                                timeout_s: t("Timeout"),
                                rise: t("Rise"),
                                fall: t("Fall"),
                              }[k]
                            }
                            type="number"
                            className={INPUT}
                            value={s.monitor[k]}
                            onChange={(e) =>
                              set({ monitor: { ...s.monitor, [k]: Number(e.target.value) } })
                            }
                          />
                        </Field>
                      ))}
                      <p className="text-xs text-muted-foreground md:col-span-2">
                        {udp
                          ? t(
                              "UDP checks have no rise count: a member comes back after its next passing check.",
                            )
                          : t(
                              "Up after {r} passing checks, down after {f} failing ones: about {s} s to mark a member down.",
                              {
                                r: s.monitor.rise,
                                f: s.monitor.fall,
                                s: downAfterSeconds(s.monitor),
                              },
                            )}
                      </p>
                    </div>
                  )}
                </>
              )}
            </section>
          )}

          {step === 6 && built && (
            <section className="space-y-5">
              <h1 className="text-xl font-semibold">{t("Review")}</h1>
              <p className="text-sm text-muted-foreground">
                {t("These objects are added to your draft as one atomic set of ops.")}
              </p>
              <div className="rounded-lg border bg-card p-4 text-sm">
                {t("Adds to")} <strong>{draft?.summary ?? t("a new draft")}</strong> ·{" "}
                {t("{n} ops", { n: opCount })}
              </div>
              <ul className="divide-y rounded-lg border bg-card text-sm">
                {built.ops.map((o, i) => (
                  <li key={i} className="flex gap-3 px-4 py-2.5">
                    <span className="w-32 text-muted-foreground">
                      {o.object_type.replace("_", " ")}
                    </span>
                    <span className="font-medium">
                      {String((o.body as { name?: string }).name)}
                    </span>
                  </li>
                ))}
              </ul>
              <p className="text-xs text-muted-foreground">
                {t(
                  "Nothing is checked until the ops are saved. After you add them, validator findings appear as a problem count in the change-set bar, on the Impact tab of Review and apply, and on this service's tabs if the draft is invalid. Nothing goes live until it is applied.",
                )}
              </p>
            </section>
          )}

          {problem != null && <ErrorState error={problem} />}

          <div className="flex items-center gap-3 border-t pt-4">
            <Button
              type="button"
              variant="outline"
              disabled={step === 1 || saving}
              onClick={() => setStep(step - 1)}
            >
              {t("Back")}
            </Button>
            {step < 6 ? (
              <Button type="button" onClick={() => void next()}>
                {t("Next: {step}", { step: t(titles[step] ?? "") })}
              </Button>
            ) : (
              <Tip label={create.reason}>
                <Button
                  type="button"
                  disabled={!create.allowed || saving}
                  onClick={() => void finish()}
                >
                  {t("Add to change set")}
                </Button>
              </Tip>
            )}
          </div>
        </main>

        <aside className="hidden w-60 shrink-0 space-y-2 text-sm lg:block">
          <h2 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
            {t("So far")}
          </h2>
          <dl className="space-y-1">
            {s.name && (
              <div className="flex justify-between gap-2">
                <dt className="text-muted-foreground">{t("Name")}</dt>
                <dd className="font-mono">{s.name}</dd>
              </div>
            )}
            {groupName && (
              <div className="flex justify-between gap-2">
                <dt className="text-muted-foreground">{t("SE group")}</dt>
                <dd>{groupName}</dd>
              </div>
            )}
            {s.vip && (
              <div className="flex justify-between gap-2">
                <dt className="text-muted-foreground">{t("VIP")}</dt>
                <dd className="font-mono">
                  {s.vip}
                  {suggested ? ` (${t("suggested")})` : ""}
                </dd>
              </div>
            )}
          </dl>
        </aside>
      </div>
    </div>
  );
}
