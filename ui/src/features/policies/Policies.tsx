// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from "@tanstack/react-query";
import { cidrProblem, isIPv4 } from "@/lib/ip";
import { ArrowDown, ArrowUp, X } from "lucide-react";
import { useMemo, useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { DraftMarker, StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/tooltip";
import { useSaveOp } from "@/features/objects/save";
import { emptyMatch, ExtraMatch } from "./MatchEditor";
import { RULE_KIND_LABEL, RulePolicySheet, type RuleKind, type RulePolicy } from "./PolicyRules";
import {
  SECURITY_LABEL,
  SecurityPolicySheet,
  type SecurityKind,
  type SecurityPolicy,
} from "./SecurityPolicies";
import { useDraft } from "@/features/shell/draft";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type Policy =
  | components["schemas"]["RedirectPolicySetRead"]
  | components["schemas"]["AllowListPolicySetRead"]
  | RulePolicy
  | SecurityPolicy
  | components["schemas"]["DirectoryPolicySetRead"]
  | components["schemas"]["PosturePolicySetRead"];
type Kind = "redirect" | "allow_list" | RuleKind | SecurityKind;
const SECURITY_KINDS: SecurityKind[] = ["access", "jwt", "client_cert", "waf", "protection"];
type Rule = components["schemas"]["RedirectRule"];
const CODES = [301, 302, 303, 307, 308] as const;
const CODE_NAMES: Record<(typeof CODES)[number], string> = {
  301: "Moved permanently",
  302: "Found",
  303: "See other",
  307: "Temporary",
  308: "Permanent",
};
const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

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
    const problem = cidrProblem(l);
    if (problem) {
      const v4 = isIPv4(l.split("/")[0] ?? "");
      return {
        cidrs,
        error: {
          line: i + 1,
          message:
            problem === "prefix"
              ? v4
                ? "prefix length must be 0–32 for IPv4"
                : "prefix length must be 0–128 for IPv6"
              : "not a valid address or range",
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

type ClassicPolicy = Extract<Policy, { kind: "redirect" | "allow_list" }>;

function PolicySheet({
  tenant,
  policy,
  kind,
  usedBy,
  onClose,
}: {
  tenant: string;
  policy: ClassicPolicy | null;
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
      : [{ match: emptyMatch(), redirect: { location: "", code: 301 } }],
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
  function reset() {
    setErrors({});
    setProblem(null);
    setName(policy?.name ?? "");
    setRules(
      policy?.kind === "redirect"
        ? structuredClone(policy.rules)
        : [{ match: emptyMatch(), redirect: { location: "", code: 301 } }],
    );
    setCidrText(policy?.kind === "allow_list" ? policy.cidrs.join("\n") : "");
  }

  return (
    <>
      <div className="space-y-4">
        <section
          aria-label={policy ? policy.name : t("New policy")}
          className="rounded-lg border bg-card"
        >
          <div className="flex items-center gap-3 border-b px-5 py-3">
            <h2 className="text-base font-semibold">
              {policy
                ? policy.name
                : kind === "redirect"
                  ? t("New redirect policy")
                  : t("New allow list")}
            </h2>
            <StatusPill status="neutral">
              {kind === "redirect" ? t("Redirect") : t("Allow list")}
            </StatusPill>
            {managed && <StatusPill status="neutral">{t("Managed by Cloudflare sync")}</StatusPill>}
            {policy && policy.draft_status !== "unchanged" && (
              <DraftMarker status={policy.draft_status} />
            )}
            {kind === "redirect" && (
              <Button
                size="sm"
                variant="outline"
                className="ml-auto"
                disabled={dis}
                onClick={() =>
                  setRules([
                    ...rules,
                    { match: emptyMatch(), redirect: { location: "", code: 301 } },
                  ])
                }
              >
                {t("Add rule")}
              </Button>
            )}
          </div>
          <div className="space-y-5 p-5">
            {problem != null && <ErrorState error={problem} />}
            {managed && (
              <p className="text-sm text-muted-foreground">
                {t(
                  "This list is kept in sync with Cloudflare's published ranges, so it is read-only here. Attach it to a virtual service to accept traffic only through Cloudflare.",
                )}
              </p>
            )}
            {!policy && (
              <label className="block max-w-sm space-y-1.5 text-sm font-medium">
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
            )}
            {kind === "redirect" ? (
              <section className="space-y-3">
                <p className="text-xs text-muted-foreground">
                  {t(
                    "Rules run in order, before the virtual service's routes. The first match redirects.",
                  )}
                </p>
                {rules.map((r, i) => {
                  const mode = r.match.path_exact
                    ? "exact"
                    : r.match.path_prefix
                      ? "prefix"
                      : "any";
                  const label = "mb-1.5 block text-sm font-medium";
                  const ruleErrors = Object.entries(errors).filter(
                    ([p]) => p.startsWith(`rules.${i}`) || p.startsWith(`rules[${i}]`),
                  );
                  return (
                    <div key={i} className="space-y-3 rounded-lg border p-4 text-sm">
                      <div className="flex items-center gap-1">
                        <span className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
                          {t("Rule {n}", { n: i + 1 })}
                        </span>
                        <span className="ml-auto" />
                        {i > 0 && (
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={dis}
                            aria-label={t("Move rule {n} up", { n: i + 1 })}
                            onClick={() => setRules(swap(rules, i, i - 1))}
                          >
                            <ArrowUp className="size-4" />
                          </Button>
                        )}
                        {i < rules.length - 1 && (
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={dis}
                            aria-label={t("Move rule {n} down", { n: i + 1 })}
                            onClick={() => setRules(swap(rules, i, i + 1))}
                          >
                            <ArrowDown className="size-4" />
                          </Button>
                        )}
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={dis || rules.length === 1}
                          aria-label={t("Remove rule {n}", { n: i + 1 })}
                          onClick={() => setRules(rules.filter((_, j) => j !== i))}
                        >
                          <X className="size-4" />
                        </Button>
                      </div>
                      <div className="grid gap-3 md:grid-cols-[1fr_0.9fr_1.2fr]">
                        <label>
                          <span className={label}>{t("When host is")}</span>
                          <input
                            aria-label={t("Rule {n} host", { n: i + 1 })}
                            className={INPUT}
                            disabled={dis}
                            value={r.match.host ?? ""}
                            onChange={(e) =>
                              setRule(i, { match: { ...r.match, host: e.target.value || null } })
                            }
                          />
                        </label>
                        <label>
                          <span className={label}>{t("and path")}</span>
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
                        </label>
                        <label>
                          <span className={label}>{t("Path")}</span>
                          <input
                            aria-label={t("Rule {n} path", { n: i + 1 })}
                            className={cn(INPUT, "font-mono")}
                            disabled={dis || mode === "any"}
                            placeholder={mode === "any" ? "—" : undefined}
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
                        </label>
                      </div>
                      <div className="grid gap-3 md:grid-cols-[1.6fr_1.1fr]">
                        <label>
                          <span className={label}>{t("Redirect to")}</span>
                          <input
                            aria-label={t("Rule {n} redirect to", { n: i + 1 })}
                            className={cn(
                              INPUT,
                              "font-mono",
                              ruleErrors.length > 0 && "border-status-down",
                            )}
                            disabled={dis}
                            value={r.redirect.location}
                            onChange={(e) =>
                              setRule(i, {
                                redirect: { ...r.redirect, location: e.target.value },
                              })
                            }
                          />
                        </label>
                        <label>
                          <span className={label}>{t("Code")}</span>
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
                                {c} {t(CODE_NAMES[c])}
                              </option>
                            ))}
                          </select>
                        </label>
                      </div>
                      <ExtraMatch
                        id={`Rule ${i + 1}`}
                        match={r.match}
                        disabled={dis}
                        onChange={(m) => setRule(i, { match: m })}
                      />
                      {ruleErrors.map(([p, m]) => (
                        <p key={p} role="alert" className="text-xs text-status-down">
                          {m} · <span className="font-mono">{p}</span>
                        </p>
                      ))}
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
          </div>
        </section>
        {!managed && (
          <div className="flex items-center justify-end gap-2">
            {policy && (
              <Tip label={write.reason}>
                <Button
                  variant="outline"
                  className="mr-auto"
                  disabled={!write.allowed}
                  onClick={() => setConfirmDelete(true)}
                >
                  {t("Delete")}
                </Button>
              </Tip>
            )}
            <Button variant="ghost" onClick={policy ? reset : onClose}>
              {policy ? t("Revert") : t("Cancel")}
            </Button>
            <Tip label={write.reason}>
              <Button
                disabled={!write.allowed || !dirty || save.isPending}
                onClick={() => void submit()}
              >
                {t("Save to draft")}
              </Button>
            </Tip>
          </div>
        )}
      </div>
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
  const [filter, setFilter] = useState<"all" | Kind>("all");
  const [editing, setEditing] = useState<{ policy: Policy | null; kind: Kind } | null>(null);
  const usedBy = (id: string) =>
    (services.data?.items ?? [])
      .filter((v) => v.policy_set_ids.includes(id) || v.allow_list_id === id)
      .map((v) => v.name);
  const rows = (list.data?.items ?? []).filter((p) => filter === "all" || p.kind === filter);
  const first = rows[0];
  const picked =
    editing ?? (first && list.data ? { policy: first, kind: first.kind as Kind } : null);
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1 basis-80">
          <h1 className="text-2xl font-semibold">{t("Policies")}</h1>
          <p className="text-sm text-muted-foreground">
            {t(
              "Redirects, rewrites, fixed responses, rate limits, logins, tokens, client certificates, WAF and bot protection, attached to virtual services",
            )}
          </p>
        </div>
        <div className="flex flex-wrap justify-end gap-2">
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
              variant="outline"
              disabled={!write.allowed}
              onClick={() => setEditing({ policy: null, kind: "rewrite" })}
            >
              {t("New rewrite policy")}
            </Button>
          </Tip>
          <Tip label={write.reason}>
            <Button
              variant="outline"
              disabled={!write.allowed}
              onClick={() => setEditing({ policy: null, kind: "respond" })}
            >
              {t("New fixed response")}
            </Button>
          </Tip>
          <Tip label={write.reason}>
            <Button
              variant="outline"
              disabled={!write.allowed}
              onClick={() => setEditing({ policy: null, kind: "error_pages" })}
            >
              {t("New error pages")}
            </Button>
          </Tip>
          <Tip label={write.reason}>
            <Button
              variant="outline"
              disabled={!write.allowed}
              onClick={() => setEditing({ policy: null, kind: "rate_limit" })}
            >
              {t("New rate limit")}
            </Button>
          </Tip>
          <Tip label={write.reason}>
            <select
              aria-label={t("New security policy")}
              className="h-9 rounded-md border border-input bg-card px-3 text-sm"
              disabled={!write.allowed}
              value=""
              onChange={(e) =>
                e.target.value && setEditing({ policy: null, kind: e.target.value as Kind })
              }
            >
              <option value="">{t("New security policy…")}</option>
              {SECURITY_KINDS.map((k) => (
                <option key={k} value={k}>
                  {t(SECURITY_LABEL[k])}
                </option>
              ))}
            </select>
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
      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,19rem)_minmax(0,1fr)]">
        <div className="space-y-3 rounded-lg border bg-card p-2">
          <div role="group" aria-label={t("Kind")} className="flex flex-wrap gap-1">
            {(
              [
                "all",
                "redirect",
                "allow_list",
                "rewrite",
                "respond",
                "error_pages",
                "rate_limit",
                ...SECURITY_KINDS,
              ] as const
            ).map((k) => (
              <button
                key={k}
                type="button"
                aria-pressed={filter === k}
                onClick={() => setFilter(k)}
                className="rounded-md border px-2.5 py-1 text-sm whitespace-nowrap hover:bg-accent/50 aria-pressed:bg-accent"
              >
                {k === "all"
                  ? t("All")
                  : k === "redirect"
                    ? t("Redirect")
                    : k === "allow_list"
                      ? t("Allow list")
                      : k in RULE_KIND_LABEL
                        ? t(RULE_KIND_LABEL[k as RuleKind])
                        : t(SECURITY_LABEL[k as SecurityKind])}
              </button>
            ))}
          </div>
          {list.isPending ? (
            <TableSkeleton cols={1} />
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
            <ul className="space-y-1">
              {rows.map((p) => {
                const active = picked?.policy?.id === p.id;
                const users = usedBy(p.id).join(", ");
                const summary =
                  p.kind === "redirect"
                    ? t("Redirect · {n} rules", { n: p.rules.length })
                    : p.kind === "rewrite"
                      ? t("Rewrite · {n} rules", { n: p.rules.length })
                      : p.kind === "respond"
                        ? t("Fixed response · {n} rules", { n: p.rules.length })
                        : p.kind === "error_pages"
                          ? t("Error pages · {n}", { n: p.pages.length })
                          : p.kind === "rate_limit"
                            ? t("Rate limit · {n} rules", { n: p.rules.length })
                            : p.kind === "access" ||
                                p.kind === "jwt" ||
                                p.kind === "client_cert" ||
                                p.kind === "waf" ||
                                p.kind === "protection"
                              ? t(SECURITY_LABEL[p.kind])
                              : p.kind === "directory"
                                ? t("Directory login · {c}", { c: p.connector.type })
                                : p.kind === "posture"
                                  ? t("Device posture · {r}", { r: p.pki_role })
                                  : p.managed_by
                                    ? t("Allow list · managed")
                                    : t("Allow list · {n} CIDRs", { n: p.cidrs.length });
                return (
                  <li key={p.id}>
                    <button
                      type="button"
                      aria-label={p.name}
                      aria-current={active}
                      onClick={() => setEditing({ policy: p, kind: p.kind as Kind })}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-md px-3 py-2 text-left hover:bg-accent/50",
                        active && "bg-accent",
                      )}
                    >
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-2 text-sm font-medium">
                          {p.draft_status !== "unchanged" && (
                            <DraftMarker status={p.draft_status} />
                          )}
                          <span className={cn(p.draft_status === "deleted" && "line-through")}>
                            {p.name}
                          </span>
                        </span>
                        <span className="block truncate text-xs text-muted-foreground">
                          {users ? `${summary} · ${users}` : summary}
                        </span>
                      </span>
                      {p.kind === "allow_list" && p.managed_by && (
                        <StatusPill status="neutral">{t("Managed")}</StatusPill>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
        {picked &&
          (picked.kind === "rewrite" ||
            picked.kind === "respond" ||
            picked.kind === "error_pages" ||
            picked.kind === "rate_limit") && (
            <RulePolicySheet
              key={
                picked.policy
                  ? `${picked.policy.id}-${picked.policy.version}-${picked.policy.draft_status}`
                  : picked.kind
              }
              tenant={tenant}
              policy={picked.policy as RulePolicy | null}
              kind={picked.kind}
              usedBy={picked.policy ? usedBy(picked.policy.id) : []}
              onClose={() => setEditing(null)}
            />
          )}
        {picked && SECURITY_KINDS.includes(picked.kind as SecurityKind) && (
          <SecurityPolicySheet
            key={
              picked.policy
                ? `${picked.policy.id}-${picked.policy.version}-${picked.policy.draft_status}`
                : picked.kind
            }
            tenant={tenant}
            policy={picked.policy as SecurityPolicy | null}
            kind={picked.kind as SecurityKind}
            usedBy={picked.policy ? usedBy(picked.policy.id) : []}
            onClose={() => setEditing(null)}
          />
        )}
        {picked && (picked.kind === "redirect" || picked.kind === "allow_list") && (
          <PolicySheet
            key={
              picked.policy
                ? `${picked.policy.id}-${picked.policy.version}-${picked.policy.draft_status}`
                : picked.kind
            }
            tenant={tenant}
            policy={picked.policy as ClassicPolicy | null}
            kind={picked.kind}
            usedBy={picked.policy ? usedBy(picked.policy.id) : []}
            onClose={() => setEditing(null)}
          />
        )}
      </div>
    </div>
  );
}
