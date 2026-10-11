import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { DraftMarker, StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Tip } from "@/components/ui/tooltip";
import { useSaveOp } from "@/features/objects/save";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Policy =
  components["schemas"]["RedirectPolicySetRead"] | components["schemas"]["AllowListPolicySetRead"];
type Rule = components["schemas"]["RedirectRule"];
const CODES = [301, 302, 303, 307, 308] as const;
const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

const V4 = /^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}(\/(\d|[12]\d|3[0-2]))?$/;
const V6 = /^[0-9a-fA-F:]+(\/(\d{1,2}|1[01]\d|12[0-8]))?$/;

/** One CIDR per line; returns the cleaned list and the first problem with its 1-based line, if any. */
export function parseCidrs(text: string): {
  cidrs: string[];
  error?: { line: number; message: string };
} {
  const lines = text.split("\n").map((l) => l.trim());
  const cidrs: string[] = [];
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i]!;
    if (!l) continue;
    if (!(V4.test(l) || (l.includes(":") && V6.test(l)))) {
      const bad = /^\d+\.\d+\.\d+\.\d+\/(\d+)$/.exec(l);
      return {
        cidrs,
        error: {
          line: i + 1,
          message: bad ? "prefix length must be 0–32 for IPv4" : "not a valid address or range",
        },
      };
    }
    cidrs.push(l);
  }
  if (cidrs.length === 0) return { cidrs, error: { line: 1, message: "add at least one range" } };
  return { cidrs };
}

function usePolicies(tenant: string, cs?: string) {
  return useQuery({
    queryKey: ["t", tenant, "policy-sets", { cs }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/policy-sets", {
          params: { path: { t: tenant }, query: { limit: 200, change_set: cs } },
        }),
      ),
  });
}
function useServices(tenant: string, cs?: string) {
  return useQuery({
    queryKey: ["t", tenant, "virtual-services", "all", { cs }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/virtual-services", {
          params: { path: { t: tenant }, query: { limit: 200, change_set: cs } },
        }),
      ),
  });
}

function PolicySheet({
  tenant,
  policy,
  kind,
  usedBy,
  onClose,
}: {
  tenant: string;
  policy: Policy | null;
  kind: "redirect" | "allow_list";
  usedBy: string[];
  onClose: () => void;
}) {
  const save = useSaveOp(tenant, "policy_set");
  const can = useCan(tenant);
  const write = can("write", "application");
  const managed = policy?.kind === "allow_list" && !!policy.managed_by;
  const [name, setName] = useState(policy?.name ?? "");
  const [rules, setRules] = useState<Rule[]>(
    policy?.kind === "redirect"
      ? structuredClone(policy.rules)
      : [{ match: {}, redirect: { location: "", code: 301 } }],
  );
  const [cidrText, setCidrText] = useState(
    policy?.kind === "allow_list" ? policy.cidrs.join("\n") : "",
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<unknown>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const cidr = useMemo(() => parseCidrs(cidrText), [cidrText]);

  const body = kind === "redirect" ? { kind, name, rules } : { kind, name, cidrs: cidr.cidrs };
  const original = policy
    ? JSON.stringify(
        policy.kind === "redirect"
          ? { kind, name: policy.name, rules: policy.rules }
          : { kind, name: policy.name, cidrs: policy.cidrs },
      )
    : "";
  const dirty = policy ? JSON.stringify(body) !== original : name !== "";
  const setRule = (i: number, p: Partial<Rule>) =>
    setRules(rules.map((r, j) => (j === i ? { ...r, ...p } : r)));

  async function submit() {
    setProblem(null);
    setErrors({});
    if (kind === "allow_list" && cidr.error) {
      setErrors({ cidrs: `Line ${cidr.error.line}: ${cidr.error.message}` });
      return;
    }
    try {
      if (!policy) await save.mutateAsync({ action: "create", body });
      else {
        const patch: Record<string, unknown> = {};
        if (name !== policy.name) patch.name = name;
        if (
          kind === "redirect" &&
          policy.kind === "redirect" &&
          JSON.stringify(rules) !== JSON.stringify(policy.rules)
        )
          patch.rules = rules;
        if (
          kind === "allow_list" &&
          policy.kind === "allow_list" &&
          JSON.stringify(cidr.cidrs) !== JSON.stringify(policy.cidrs)
        )
          patch.cidrs = cidr.cidrs;
        await save.mutateAsync({
          action: "update",
          id: policy.id,
          version: policy.version,
          body: patch,
        });
      }
      onClose();
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        const m: Record<string, string> = {};
        for (const x of e.problem?.errors ?? []) m[x.path] = x.message;
        setErrors(m);
      } else setProblem(e);
    }
  }
  const dis = !write.allowed || managed;

  return (
    <>
      <Sheet
        inline
        open
        onOpenChange={(o) => !o && onClose()}
        title={
          policy
            ? policy.name
            : kind === "redirect"
              ? t("New redirect policy")
              : t("New allow list")
        }
        badge={
          managed ? (
            <StatusPill status="neutral">{t("Managed by Cloudflare sync")}</StatusPill>
          ) : policy && policy.draft_status !== "unchanged" ? (
            <DraftMarker status={policy.draft_status} />
          ) : undefined
        }
        footer={
          managed ? (
            <Button variant="ghost" className="ml-auto" onClick={onClose}>
              {t("Close")}
            </Button>
          ) : (
            <>
              {policy && (
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
          )
        }
      >
        {problem != null && <ErrorState error={problem} />}
        {managed && (
          <p className="text-sm text-muted-foreground">
            {t(
              "This list is kept in sync with Cloudflare's published ranges, so it is read-only here. Attach it to a virtual service to accept traffic only through Cloudflare.",
            )}
          </p>
        )}
        <label className="block space-y-1.5 text-sm font-medium">
          {t("Name")}
          <input
            aria-label={t("Name")}
            className={INPUT}
            disabled={dis}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          {errors.name && (
            <span role="alert" className="block text-xs font-normal text-status-down">
              {errors.name}
            </span>
          )}
        </label>
        {kind === "redirect" ? (
          <section className="space-y-3">
            <div className="flex items-center">
              <h3 className="text-sm font-medium">{t("Rules")}</h3>
              <Button
                size="sm"
                variant="outline"
                className="ml-auto"
                disabled={dis}
                onClick={() =>
                  setRules([...rules, { match: {}, redirect: { location: "", code: 301 } }])
                }
              >
                {t("Add rule")}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {t(
                "Rules run in order, before the virtual service's routes. The first match redirects.",
              )}
            </p>
            {rules.map((r, i) => {
              const mode = r.match.path_exact ? "exact" : r.match.path_prefix ? "prefix" : "any";
              return (
                <div key={i} className="space-y-2 rounded-lg border p-3 text-sm">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{t("Rule {n}", { n: i + 1 })}</span>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={dis || i === 0}
                      aria-label={t("Move rule {n} up", { n: i + 1 })}
                      onClick={() => setRules(swap(rules, i, i - 1))}
                    >
                      ↑
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={dis || i === rules.length - 1}
                      aria-label={t("Move rule {n} down", { n: i + 1 })}
                      onClick={() => setRules(swap(rules, i, i + 1))}
                    >
                      ↓
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="ml-auto"
                      disabled={dis || rules.length === 1}
                      onClick={() => setRules(rules.filter((_, j) => j !== i))}
                    >
                      {t("Remove")}
                    </Button>
                  </div>
                  <input
                    aria-label={t("Rule {n} host", { n: i + 1 })}
                    placeholder={t("When host is")}
                    className={INPUT}
                    disabled={dis}
                    value={r.match.host ?? ""}
                    onChange={(e) =>
                      setRule(i, { match: { ...r.match, host: e.target.value || null } })
                    }
                  />
                  <div className="grid grid-cols-[9rem_1fr] gap-2">
                    <select
                      aria-label={t("Rule {n} path match", { n: i + 1 })}
                      className={INPUT}
                      disabled={dis}
                      value={mode}
                      onChange={(e) => {
                        const v = e.target.value;
                        const p = r.match.path_exact ?? r.match.path_prefix ?? "";
                        setRule(i, {
                          match: {
                            ...r.match,
                            path_exact: v === "exact" ? p || "/" : null,
                            path_prefix: v === "prefix" ? p || "/" : null,
                          },
                        });
                      }}
                    >
                      <option value="any">{t("Any path")}</option>
                      <option value="prefix">{t("Starts with")}</option>
                      <option value="exact">{t("Is exactly")}</option>
                    </select>
                    <input
                      aria-label={t("Rule {n} path", { n: i + 1 })}
                      className={cn(INPUT, "font-mono")}
                      disabled={dis || mode === "any"}
                      value={r.match.path_exact ?? r.match.path_prefix ?? ""}
                      onChange={(e) =>
                        setRule(i, {
                          match: {
                            ...r.match,
                            [mode === "exact" ? "path_exact" : "path_prefix"]: e.target.value,
                          },
                        })
                      }
                    />
                  </div>
                  <input
                    aria-label={t("Rule {n} redirect to", { n: i + 1 })}
                    placeholder={t("Redirect to")}
                    className={cn(INPUT, "font-mono")}
                    disabled={dis}
                    value={r.redirect.location}
                    onChange={(e) =>
                      setRule(i, { redirect: { ...r.redirect, location: e.target.value } })
                    }
                  />
                  {Object.entries(errors)
                    .filter(([p]) => p.startsWith(`rules.${i}`) || p.startsWith(`rules[${i}]`))
                    .map(([p, m]) => (
                      <p key={p} role="alert" className="text-xs text-status-down">
                        {m} · {p}
                      </p>
                    ))}
                  <select
                    aria-label={t("Rule {n} code", { n: i + 1 })}
                    className={INPUT}
                    disabled={dis}
                    value={r.redirect.code}
                    onChange={(e) =>
                      setRule(i, {
                        redirect: {
                          ...r.redirect,
                          code: Number(e.target.value) as Rule["redirect"]["code"],
                        },
                      })
                    }
                  >
                    {CODES.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </select>
                </div>
              );
            })}
          </section>
        ) : (
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Allowed source ranges")}
            <textarea
              aria-label={t("Allowed source ranges")}
              className="h-40 w-full rounded-md border bg-card p-3 font-mono text-xs disabled:opacity-60"
              disabled={dis}
              value={cidrText}
              onChange={(e) => setCidrText(e.target.value)}
            />
            {(errors.cidrs || (cidrText && cidr.error)) && (
              <span role="alert" className="block text-xs font-normal text-status-down">
                {errors.cidrs ?? `Line ${cidr.error!.line}: ${cidr.error!.message}`}
              </span>
            )}
            <span className="block text-xs font-normal text-muted-foreground">
              {t("One CIDR per line. IPv4 and IPv6. At least one range.")}
            </span>
          </label>
        )}
        {policy && (
          <p className="text-xs text-muted-foreground">
            {usedBy.length
              ? t("Used by {names}", { names: usedBy.join(", ") })
              : t("Not used by any virtual service.")}
          </p>
        )}
      </Sheet>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete {name}?", { name: policy?.name ?? "" })}
        confirmLabel={t("Add delete to draft")}
        busy={save.isPending}
        onConfirm={async () => {
          if (!policy) return;
          try {
            await save.mutateAsync({ action: "delete", id: policy.id, version: policy.version });
            onClose();
          } catch (e) {
            setProblem(e);
          } finally {
            setConfirmDelete(false);
          }
        }}
      >
        <p>
          {usedBy.length
            ? t("{n} services use this policy; the validator blocks the apply until they stop.", {
                n: usedBy.length,
              })
            : t("No virtual service uses this policy.")}
        </p>
      </ConfirmDialog>
    </>
  );
}

function swap<T>(a: T[], i: number, j: number): T[] {
  const out = [...a];
  [out[i], out[j]] = [out[j]!, out[i]!];
  return out;
}

/** Redirect policies and source allow lists, attached to virtual services. */
export function Policies({ tenant }: { tenant: string }) {
  const { draft } = useDraft(tenant);
  const list = usePolicies(tenant, draft?.id);
  const services = useServices(tenant, draft?.id);
  const can = useCan(tenant);
  const write = can("write", "application");
  const [filter, setFilter] = useState<"all" | "redirect" | "allow_list">("all");
  const [editing, setEditing] = useState<{
    policy: Policy | null;
    kind: "redirect" | "allow_list";
  } | null>(null);
  const usedBy = (id: string) =>
    (services.data?.items ?? [])
      .filter((v) => v.policy_set_ids.includes(id) || v.allow_list_id === id)
      .map((v) => v.name);
  const rows = (list.data?.items ?? []).filter((p) => filter === "all" || p.kind === filter);
  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("Policies")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("Redirect rules and source allow lists, attached to virtual services")}
          </p>
        </div>
        <div className="flex gap-2">
          <Tip label={write.reason}>
            <Button
              variant="outline"
              disabled={!write.allowed}
              onClick={() => setEditing({ policy: null, kind: "allow_list" })}
            >
              {t("New allow list")}
            </Button>
          </Tip>
          <Tip label={write.reason}>
            <Button
              disabled={!write.allowed}
              onClick={() => setEditing({ policy: null, kind: "redirect" })}
            >
              {t("New redirect policy")}
            </Button>
          </Tip>
        </div>
      </div>
      <div
        role="group"
        aria-label={t("Kind")}
        className="inline-flex overflow-hidden rounded-md border"
      >
        {(["all", "redirect", "allow_list"] as const).map((k) => (
          <button
            key={k}
            type="button"
            aria-pressed={filter === k}
            onClick={() => setFilter(k)}
            className="px-3 py-1.5 text-sm aria-pressed:bg-accent"
          >
            {{ all: t("All"), redirect: t("Redirect"), allow_list: t("Allow list") }[k]}
          </button>
        ))}
      </div>
      <div className={cn("grid gap-4", editing && "lg:grid-cols-[minmax(0,22rem)_1fr]")}>
        <div className="self-start rounded-lg border bg-card">
          {list.isPending ? (
            <TableSkeleton cols={3} />
          ) : list.isError ? (
            <ErrorState error={list.error} onRetry={() => void list.refetch()} />
          ) : rows.length === 0 ? (
            <EmptyState
              title={t("No policies yet")}
              body={t(
                "A policy redirects requests or limits which source addresses a virtual service accepts.",
              )}
            />
          ) : (
            <ul className="divide-y">
              {rows.map((p) => (
                <li
                  key={p.id}
                  className={cn(
                    "flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-[13px]",
                    editing?.policy?.id === p.id && "bg-accent/50",
                  )}
                >
                  {p.draft_status !== "unchanged" && <DraftMarker status={p.draft_status} />}
                  <button
                    type="button"
                    className={cn(
                      "font-medium text-primary hover:underline",
                      p.draft_status === "deleted" && "line-through",
                    )}
                    onClick={() =>
                      setEditing({ policy: p, kind: p.kind as "redirect" | "allow_list" })
                    }
                  >
                    {p.name}
                  </button>
                  <span className="text-muted-foreground">
                    {p.kind === "redirect"
                      ? t("Redirect · {n} rules", { n: p.rules.length })
                      : p.managed_by
                        ? t("Allow list · managed")
                        : t("Allow list · {n} CIDRs", { n: p.cidrs.length })}
                  </span>
                  {p.kind === "allow_list" && p.managed_by && (
                    <StatusPill status="neutral">{t("Managed")}</StatusPill>
                  )}
                  <span className={cn("text-muted-foreground", !editing && "ml-auto")}>
                    {usedBy(p.id).join(", ")}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        {editing && (
          <PolicySheet
            key={
              editing.policy
                ? `${editing.policy.id}-${editing.policy.version}-${editing.policy.draft_status}`
                : editing.kind
            }
            tenant={tenant}
            policy={editing.policy}
            kind={editing.kind}
            usedBy={editing.policy ? usedBy(editing.policy.id) : []}
            onClose={() => setEditing(null)}
          />
        )}
      </div>
    </div>
  );
}
