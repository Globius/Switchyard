// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Editors for the rule-based policy kinds (ADR 0102, 0103): header and URL rewrite, fixed
 * responses, and custom error pages. Saved into the draft like every other object.
 */
import { ArrowDown, ArrowUp, X } from "lucide-react";
import { useState } from "react";
import { ApiError } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { ErrorState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { useSaveOp } from "@/features/objects/save";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { cleanMatch, emptyMatch, MatchEditor } from "./MatchEditor";

export type RuleKind = "rewrite" | "respond" | "error_pages" | "rate_limit";
export type RulePolicy =
  | components["schemas"]["RewritePolicySetRead"]
  | components["schemas"]["RespondPolicySetRead"]
  | components["schemas"]["ErrorPagesPolicySetRead"]
  | components["schemas"]["RateLimitPolicySetRead"];
type RewriteRule = components["schemas"]["RewriteRule"];
type ResponseRule = components["schemas"]["ResponseRule"];
type ErrorPage = components["schemas"]["ErrorPage"];
type LimitRule = components["schemas"]["RateLimitRule"];
type Variable = components["schemas"]["HeaderVariable"];
type ActionType = "set_header" | "add_header" | "remove_header" | "replace_prefix";

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";
const VARIABLES: Variable[] = [
  "client_ip",
  "client_port",
  "host",
  "vip",
  "vip_port",
  "method",
  "tls_version",
  "tls_cipher",
  "tls_sni",
  "request_id",
];
/** What each variable holds, for the picker. */
export const VARIABLE_HELP: Record<Variable, string> = {
  client_ip: "The client's address",
  client_port: "The client's source port",
  host: "The Host the client asked for",
  vip: "The virtual service's address",
  vip_port: "The port the client connected to",
  method: "The HTTP method",
  tls_version: "The negotiated TLS version",
  tls_cipher: "The negotiated cipher",
  tls_sni: "The server name sent in the TLS handshake",
  request_id: "A unique id for this request",
};
/** Headers the platform owns; the API refuses a policy that touches them. */
export const PROTECTED_HEADERS = new Set([
  "content-length",
  "transfer-encoding",
  "connection",
  "keep-alive",
  "te",
  "trailer",
  "upgrade",
  "expect",
  "proxy-authorization",
  "proxy-authenticate",
  "proxy-connection",
  "x-forwarded-for",
  "x-forwarded-proto",
]);
const STATUSES = [
  200, 202, 204, 400, 401, 403, 404, 405, 410, 429, 451, 500, 502, 503, 504,
] as const;
const PAGE_STATUSES = [400, 403, 408, 500, 502, 503, 504] as const;
const TYPES = ["text/plain", "text/html", "application/json"] as const;

export const RULE_KIND_LABEL: Record<RuleKind, string> = {
  rewrite: "Rewrite",
  respond: "Fixed response",
  error_pages: "Error pages",
  rate_limit: "Rate limit",
};

export function newRewriteRule(): RewriteRule {
  return {
    phase: "request",
    match: emptyMatch(),
    action: { type: "set_header", name: "", value: "" },
  };
}
export function newLimitRule(): LimitRule {
  return {
    match: emptyMatch(),
    key: "client_ip",
    max_requests: 100,
    max_concurrent: null,
    period_s: 10,
    table_size: 10000,
    entry_ttl_s: null,
    action: "deny",
    deny_status: 429,
    tarpit_s: 5,
  };
}

/** The rule as the API takes it: exactly one limit, and only the fields its action and key use. */
export function cleanLimit(r: LimitRule): LimitRule {
  const out: Record<string, unknown> = {
    match: cleanMatch(r.match ?? emptyMatch()),
    key: r.key,
    table_size: r.table_size,
    action: r.action,
  };
  if (r.key === "header") out.header_name = r.header_name;
  if (r.entry_ttl_s != null) out.entry_ttl_s = r.entry_ttl_s;
  if (r.max_concurrent != null) out.max_concurrent = r.max_concurrent;
  else {
    out.max_requests = r.max_requests;
    out.period_s = r.period_s;
  }
  if (r.action === "deny") out.deny_status = r.deny_status;
  if (r.action === "tarpit") out.tarpit_s = r.tarpit_s;
  return out as LimitRule;
}

export function newResponseRule(): ResponseRule {
  return { match: emptyMatch(), status: 503, content_type: "text/html", body: "" };
}

function actionType(r: RewriteRule): ActionType {
  return r.action.type as ActionType;
}

/** The rule as the API takes it: empty match fields and unused action fields dropped. */
export function cleanRewrite(r: RewriteRule): RewriteRule {
  const a = r.action;
  const action =
    a.type === "remove_header"
      ? { type: a.type, name: a.name }
      : a.type === "replace_prefix"
        ? { type: a.type, from_prefix: a.from_prefix, to_prefix: a.to_prefix }
        : a.variable
          ? { type: a.type, name: a.name, variable: a.variable }
          : { type: a.type, name: a.name, value: a.value ?? "" };
  return { phase: r.phase, match: cleanMatch(r.match ?? emptyMatch()), action } as RewriteRule;
}
export function cleanResponse(r: ResponseRule): ResponseRule {
  const out: Record<string, unknown> = {
    match: cleanMatch(r.match ?? emptyMatch()),
    status: r.status,
  };
  if (r.body) {
    out.body = r.body;
    out.content_type = r.content_type ?? "text/plain";
  }
  if (r.retry_after_s && (r.status === 429 || r.status === 503))
    out.retry_after_s = r.retry_after_s;
  return out as ResponseRule;
}

function Move({
  i,
  n,
  onSwap,
  onRemove,
  disabled,
  what,
}: {
  i: number;
  n: number;
  onSwap: (a: number, b: number) => void;
  onRemove: () => void;
  disabled?: boolean;
  what: string;
}) {
  return (
    <div className="flex items-center gap-1">
      <span className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
        {t(what)} {i + 1}
      </span>
      <span className="ml-auto" />
      {i > 0 && (
        <Button
          size="sm"
          variant="ghost"
          disabled={disabled}
          aria-label={`${t("Move up")} ${i + 1}`}
          onClick={() => onSwap(i, i - 1)}
        >
          <ArrowUp className="size-4" />
        </Button>
      )}
      {i < n - 1 && (
        <Button
          size="sm"
          variant="ghost"
          disabled={disabled}
          aria-label={`${t("Move down")} ${i + 1}`}
          onClick={() => onSwap(i, i + 1)}
        >
          <ArrowDown className="size-4" />
        </Button>
      )}
      <Button
        size="sm"
        variant="ghost"
        disabled={disabled || n === 1}
        aria-label={`${t("Remove")} ${t(what)} ${i + 1}`}
        onClick={onRemove}
      >
        <X className="size-4" />
      </Button>
    </div>
  );
}

const swap = <T,>(a: T[], i: number, j: number) => {
  const c = [...a];
  [c[i], c[j]] = [c[j]!, c[i]!];
  return c;
};

export function RulePolicySheet({
  tenant,
  policy,
  kind,
  usedBy,
  onClose,
}: {
  tenant: string;
  policy: RulePolicy | null;
  kind: RuleKind;
  usedBy: string[];
  onClose: () => void;
}) {
  const save = useSaveOp(tenant, "policy_set");
  const write = useCan(tenant)("write", "application");
  const dis = !write.allowed;
  const [previewing, setPreviewing] = useState<Record<number, boolean>>({});
  const [name, setName] = useState(policy?.name ?? "");
  const [rewrite, setRewrite] = useState<RewriteRule[]>(
    policy?.kind === "rewrite" ? structuredClone(policy.rules) : [newRewriteRule()],
  );
  const [respond, setRespond] = useState<ResponseRule[]>(
    policy?.kind === "respond" ? structuredClone(policy.rules) : [newResponseRule()],
  );
  const [limits, setLimits] = useState<LimitRule[]>(
    policy?.kind === "rate_limit" ? structuredClone(policy.rules) : [newLimitRule()],
  );
  const [pages, setPages] = useState<Record<number, ErrorPage>>(
    Object.fromEntries(
      (policy?.kind === "error_pages" ? policy.pages : []).map((p) => [p.status, { ...p }]),
    ),
  );
  const [problem, setProblem] = useState<unknown>(null);
  const [errors, setErrors] = useState<{ path: string; message: string }[]>([]);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const body =
    kind === "rewrite"
      ? { kind, name, rules: rewrite.map(cleanRewrite) }
      : kind === "respond"
        ? { kind, name, rules: respond.map(cleanResponse) }
        : kind === "rate_limit"
          ? { kind, name, rules: limits.map(cleanLimit) }
          : {
              kind,
              name,
              pages: PAGE_STATUSES.filter((s) => pages[s]).map((s) => ({
                status: s,
                content_type: pages[s]!.content_type ?? "text/plain",
                body: pages[s]!.body,
              })),
            };

  async function submit() {
    setProblem(null);
    setErrors([]);
    try {
      if (!policy) await save.mutateAsync({ action: "create", body });
      else
        await save.mutateAsync({
          action: "update",
          id: policy.id,
          version: policy.version,
          body,
        });
      onClose();
    } catch (e) {
      if (e instanceof ApiError && e.status === 422)
        setErrors((e.problem?.errors ?? []).map((x) => ({ path: x.path, message: x.message })));
      else setProblem(e);
    }
  }

  return (
    <section
      aria-label={
        policy ? policy.name : t("New {kind}", { kind: t(RULE_KIND_LABEL[kind]).toLowerCase() })
      }
      className="flex min-h-[28rem] flex-col rounded-lg border bg-card"
    >
      <div className="flex items-center gap-3 border-b px-5 py-3">
        <h2 className="text-base font-semibold">
          {policy
            ? t("Edit {name}", { name: policy.name })
            : t("New {kind}", { kind: t(RULE_KIND_LABEL[kind]).toLowerCase() })}
        </h2>
        <span className="text-xs text-muted-foreground">{t(RULE_KIND_LABEL[kind])}</span>
        <button
          type="button"
          className="ml-auto rounded-md p-1 hover:bg-accent"
          aria-label={t("Close")}
          onClick={onClose}
        >
          <X className="size-4" />
        </button>
      </div>
      <div className="flex-1 space-y-5 p-5">
        {problem != null && <ErrorState error={problem} />}
        {errors.length > 0 && (
          <ul
            role="alert"
            className="space-y-1 rounded-md border border-status-down/40 bg-status-down-bg p-3 text-xs"
          >
            {errors.map((e) => (
              <li key={e.path + e.message}>
                <span className="font-mono">{e.path}</span>: {e.message}
              </li>
            ))}
          </ul>
        )}
        {usedBy.length > 0 && (
          <p className="text-xs text-muted-foreground">
            {t("Used by {names}", { names: usedBy.join(", ") })}
          </p>
        )}
        <label className="block max-w-sm space-y-1.5 text-sm font-medium">
          {t("Name")}
          <input
            aria-label={t("Name")}
            className={INPUT}
            disabled={dis}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          {policy && (
            <span className="block text-xs font-normal text-muted-foreground">
              {t(
                "Renaming is a draft change. Virtual services attach policies by ID, so they keep working.",
              )}
            </span>
          )}
        </label>

        {kind === "rewrite" && (
          <section className="space-y-3">
            <p className="text-xs text-muted-foreground">
              {t(
                "Request rules run after redirects and fixed responses and before routing; response rules run on the way out. Rules apply in order.",
              )}
            </p>
            {rewrite.map((r, i) => {
              const at = actionType(r);
              const upd = (p: Partial<RewriteRule>) =>
                setRewrite(rewrite.map((x, j) => (j === i ? { ...x, ...p } : x)));
              const act = r.action as Record<string, string | null | undefined>;
              const setAct = (p: Record<string, unknown>) =>
                upd({ action: { ...r.action, ...p } as RewriteRule["action"] });
              return (
                <div key={i} className="space-y-3 rounded-lg border p-4 text-sm">
                  <Move
                    i={i}
                    n={rewrite.length}
                    disabled={dis}
                    what="Rule"
                    onSwap={(a, b) => setRewrite(swap(rewrite, a, b))}
                    onRemove={() => setRewrite(rewrite.filter((_, j) => j !== i))}
                  />
                  <div className="grid gap-3 md:grid-cols-2">
                    <label>
                      <span className="mb-1.5 block text-sm font-medium">{t("Phase")}</span>
                      <select
                        aria-label={`Rule ${i + 1} phase`}
                        className={INPUT}
                        disabled={dis}
                        value={r.phase ?? "request"}
                        onChange={(e) => {
                          const phase = e.target.value as "request" | "response";
                          upd({
                            phase,
                            ...(phase === "response" && at === "replace_prefix"
                              ? {
                                  action: {
                                    type: "set_header",
                                    name: "",
                                    value: "",
                                  } as RewriteRule["action"],
                                }
                              : {}),
                          });
                        }}
                      >
                        <option value="request">{t("Request (before routing)")}</option>
                        <option value="response">{t("Response (on the way out)")}</option>
                      </select>
                    </label>
                    <label>
                      <span className="mb-1.5 block text-sm font-medium">{t("Action")}</span>
                      <select
                        aria-label={`Rule ${i + 1} action`}
                        className={INPUT}
                        disabled={dis}
                        value={at}
                        onChange={(e) => {
                          const type = e.target.value as ActionType;
                          upd({
                            action:
                              type === "replace_prefix"
                                ? ({
                                    type,
                                    from_prefix: "",
                                    to_prefix: "",
                                  } as RewriteRule["action"])
                                : ({
                                    type,
                                    name: act.name ?? "",
                                    ...(type === "remove_header" ? {} : { value: "" }),
                                  } as RewriteRule["action"]),
                          });
                        }}
                      >
                        <option value="set_header">{t("Set header")}</option>
                        <option value="add_header">{t("Add header")}</option>
                        <option value="remove_header">{t("Remove header")}</option>
                        {(r.phase ?? "request") === "request" && (
                          <option value="replace_prefix">{t("Replace path prefix")}</option>
                        )}
                      </select>
                    </label>
                  </div>
                  {at === "replace_prefix" ? (
                    <div className="grid gap-3 md:grid-cols-2">
                      <label>
                        <span className="mb-1.5 block text-sm font-medium">{t("From prefix")}</span>
                        <input
                          aria-label={`Rule ${i + 1} from prefix`}
                          className={cn(INPUT, "font-mono")}
                          disabled={dis}
                          value={act.from_prefix ?? ""}
                          onChange={(e) => setAct({ from_prefix: e.target.value })}
                        />
                      </label>
                      <label>
                        <span className="mb-1.5 block text-sm font-medium">{t("To prefix")}</span>
                        <input
                          aria-label={`Rule ${i + 1} to prefix`}
                          className={cn(INPUT, "font-mono")}
                          disabled={dis}
                          value={act.to_prefix ?? ""}
                          onChange={(e) => setAct({ to_prefix: e.target.value })}
                        />
                        <span className="block text-xs text-muted-foreground">
                          {t("/ strips the prefix.")}
                        </span>
                      </label>
                    </div>
                  ) : (
                    <div className="grid gap-3 md:grid-cols-[1fr_1.4fr]">
                      <label>
                        <span className="mb-1.5 block text-sm font-medium">{t("Header name")}</span>
                        <input
                          aria-label={`Rule ${i + 1} header name`}
                          className={cn(INPUT, "font-mono")}
                          disabled={dis}
                          value={act.name ?? ""}
                          onChange={(e) => setAct({ name: e.target.value })}
                        />
                        {PROTECTED_HEADERS.has((act.name ?? "").trim().toLowerCase()) && (
                          <span role="alert" className="block text-xs text-status-down">
                            {t("This header is managed by the platform and can't be changed.")}
                          </span>
                        )}
                      </label>
                      {at !== "remove_header" && (
                        <div className="space-y-1.5">
                          <span className="block text-sm font-medium">{t("Value")}</span>
                          <div className="flex gap-2">
                            <select
                              aria-label={`Rule ${i + 1} value source`}
                              className={cn(INPUT, "w-32")}
                              disabled={dis}
                              value={act.variable ? "variable" : "literal"}
                              onChange={(e) =>
                                setAct(
                                  e.target.value === "variable"
                                    ? { variable: "client_ip", value: null }
                                    : { variable: null, value: "" },
                                )
                              }
                            >
                              <option value="literal">{t("Text")}</option>
                              <option value="variable">{t("Variable")}</option>
                            </select>
                            {act.variable ? (
                              <select
                                aria-label={`Rule ${i + 1} variable`}
                                className={INPUT}
                                disabled={dis}
                                value={act.variable}
                                onChange={(e) => setAct({ variable: e.target.value })}
                              >
                                {VARIABLES.map((v) => (
                                  <option key={v} value={v}>
                                    {v} — {t(VARIABLE_HELP[v])}
                                  </option>
                                ))}
                              </select>
                            ) : (
                              <input
                                aria-label={`Rule ${i + 1} value`}
                                className={cn(INPUT, "font-mono")}
                                disabled={dis}
                                value={act.value ?? ""}
                                onChange={(e) => setAct({ value: e.target.value })}
                              />
                            )}
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                  <div>
                    <div className="mb-1.5 text-sm font-medium">
                      {t("When the request matches")}
                    </div>
                    <MatchEditor
                      id={`Rule ${i + 1}`}
                      disabled={dis}
                      match={r.match ?? emptyMatch()}
                      onChange={(m) => upd({ match: m })}
                    />
                  </div>
                </div>
              );
            })}
            <Button
              variant="outline"
              size="sm"
              disabled={dis}
              onClick={() => setRewrite([...rewrite, newRewriteRule()])}
            >
              {t("Add rule")}
            </Button>
          </section>
        )}

        {kind === "respond" && (
          <section className="space-y-3">
            <p className="text-xs text-muted-foreground">
              {t(
                "Fixed responses answer matching requests without asking a pool; the first match wins. An empty match answers every request (a maintenance page).",
              )}
            </p>
            {respond.map((r, i) => {
              const upd = (p: Partial<ResponseRule>) =>
                setRespond(respond.map((x, j) => (j === i ? { ...x, ...p } : x)));
              return (
                <div key={i} className="space-y-3 rounded-lg border p-4 text-sm">
                  <Move
                    i={i}
                    n={respond.length}
                    disabled={dis}
                    what="Rule"
                    onSwap={(a, b) => setRespond(swap(respond, a, b))}
                    onRemove={() => setRespond(respond.filter((_, j) => j !== i))}
                  />
                  <div className="grid gap-3 md:grid-cols-3">
                    <label>
                      <span className="mb-1.5 block text-sm font-medium">{t("Status")}</span>
                      <select
                        aria-label={`Rule ${i + 1} status`}
                        className={INPUT}
                        disabled={dis}
                        value={r.status}
                        onChange={(e) =>
                          upd({ status: Number(e.target.value) as ResponseRule["status"] })
                        }
                      >
                        {STATUSES.map((s) => (
                          <option key={s} value={s}>
                            {s}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <span className="mb-1.5 block text-sm font-medium">{t("Content type")}</span>
                      <select
                        aria-label={`Rule ${i + 1} content type`}
                        className={INPUT}
                        disabled={dis}
                        value={r.content_type ?? "text/plain"}
                        onChange={(e) =>
                          upd({ content_type: e.target.value as ResponseRule["content_type"] })
                        }
                      >
                        {TYPES.map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                    </label>
                    {(r.status === 429 || r.status === 503) && (
                      <label>
                        <span className="mb-1.5 block text-sm font-medium">
                          {t("Retry-After (s)")}
                        </span>
                        <input
                          aria-label={`Rule ${i + 1} retry after`}
                          type="number"
                          min={1}
                          className={INPUT}
                          disabled={dis}
                          value={r.retry_after_s ?? ""}
                          onChange={(e) =>
                            upd({ retry_after_s: e.target.value ? Number(e.target.value) : null })
                          }
                        />
                      </label>
                    )}
                  </div>
                  <div className="block">
                    <div className="mb-1.5 flex items-center gap-2 text-sm font-medium">
                      {t("Body")}
                      <Button
                        size="sm"
                        variant={previewing[i] ? "ghost" : "outline"}
                        onClick={() => setPreviewing({ ...previewing, [i]: false })}
                      >
                        {t("Edit")}
                      </Button>
                      <Button
                        size="sm"
                        variant={previewing[i] ? "outline" : "ghost"}
                        onClick={() => setPreviewing({ ...previewing, [i]: true })}
                      >
                        {t("Preview")}
                      </Button>
                    </div>
                    {previewing[i] ? (
                      <iframe
                        title={`Rule ${i + 1} body preview`}
                        sandbox=""
                        srcDoc={r.body ?? ""}
                        className="h-40 w-full rounded-md border bg-white"
                      />
                    ) : (
                      <textarea
                        aria-label={`Rule ${i + 1} body`}
                        className={cn(INPUT, "h-28 py-2 font-mono text-xs")}
                        disabled={dis}
                        value={r.body ?? ""}
                        onChange={(e) => upd({ body: e.target.value })}
                      />
                    )}
                    <span className="block text-xs text-muted-foreground">
                      {t("Leave empty for an empty response. The preview runs no scripts.")}
                    </span>
                  </div>
                  <div>
                    <div className="mb-1.5 text-sm font-medium">
                      {t("When the request matches")}
                    </div>
                    <MatchEditor
                      id={`Rule ${i + 1}`}
                      disabled={dis}
                      match={r.match ?? emptyMatch()}
                      onChange={(m) => upd({ match: m })}
                    />
                  </div>
                </div>
              );
            })}
            <Button
              variant="outline"
              size="sm"
              disabled={dis}
              onClick={() => setRespond([...respond, newResponseRule()])}
            >
              {t("Add rule")}
            </Button>
          </section>
        )}

        {kind === "rate_limit" && (
          <section className="space-y-3">
            <p className="text-xs text-muted-foreground">
              {t(
                "Each rule counts the requests that match it, per client, per header value or per request path, and limits how often they may come. A path key is one ceiling for the whole service on this engine, shared by every client.",
              )}
            </p>
            {limits.map((r, i) => {
              const upd = (p: Partial<LimitRule>) =>
                setLimits(limits.map((x, j) => (j === i ? { ...x, ...p } : x)));
              const concurrent = r.max_concurrent != null;
              return (
                <div key={i} className="space-y-3 rounded-lg border p-4 text-sm">
                  <Move
                    i={i}
                    n={limits.length}
                    disabled={dis}
                    what="Rule"
                    onSwap={(a, b) => setLimits(swap(limits, a, b))}
                    onRemove={() => setLimits(limits.filter((_, j) => j !== i))}
                  />
                  <div className="grid gap-3 md:grid-cols-3">
                    <label>
                      <span className="mb-1.5 block text-sm font-medium">{t("Count per")}</span>
                      <select
                        aria-label={`Rule ${i + 1} key`}
                        className={INPUT}
                        disabled={dis}
                        value={r.key ?? "client_ip"}
                        onChange={(e) => upd({ key: e.target.value as LimitRule["key"] })}
                      >
                        <option value="client_ip">{t("Client address")}</option>
                        <option value="header">{t("Header value")}</option>
                        <option value="path">{t("Request path (per URL)")}</option>
                      </select>
                    </label>
                    {r.key === "header" && (
                      <label>
                        <span className="mb-1.5 block text-sm font-medium">{t("Header")}</span>
                        <input
                          aria-label={`Rule ${i + 1} key header`}
                          className={cn(INPUT, "font-mono")}
                          disabled={dis}
                          value={r.header_name ?? ""}
                          onChange={(e) => upd({ header_name: e.target.value })}
                        />
                      </label>
                    )}
                    <label>
                      <span className="mb-1.5 block text-sm font-medium">{t("Limit")}</span>
                      <select
                        aria-label={`Rule ${i + 1} limit type`}
                        className={INPUT}
                        disabled={dis}
                        value={concurrent ? "concurrent" : "rate"}
                        onChange={(e) =>
                          upd(
                            e.target.value === "concurrent"
                              ? { max_concurrent: 20, max_requests: null }
                              : { max_concurrent: null, max_requests: 100 },
                          )
                        }
                      >
                        <option value="rate">{t("Requests per period")}</option>
                        <option value="concurrent">{t("Requests in flight")}</option>
                      </select>
                    </label>
                  </div>
                  <div className="grid gap-3 md:grid-cols-3">
                    {concurrent ? (
                      <label>
                        <span className="mb-1.5 block text-sm font-medium">{t("At most")}</span>
                        <input
                          aria-label={`Rule ${i + 1} max concurrent`}
                          type="number"
                          min={1}
                          className={INPUT}
                          disabled={dis}
                          value={r.max_concurrent ?? ""}
                          onChange={(e) => upd({ max_concurrent: Number(e.target.value) })}
                        />
                      </label>
                    ) : (
                      <>
                        <label>
                          <span className="mb-1.5 block text-sm font-medium">{t("At most")}</span>
                          <input
                            aria-label={`Rule ${i + 1} max requests`}
                            type="number"
                            min={1}
                            className={INPUT}
                            disabled={dis}
                            value={r.max_requests ?? ""}
                            onChange={(e) => upd({ max_requests: Number(e.target.value) })}
                          />
                        </label>
                        <label>
                          <span className="mb-1.5 block text-sm font-medium">{t("Per")}</span>
                          <select
                            aria-label={`Rule ${i + 1} period`}
                            className={INPUT}
                            disabled={dis}
                            value={r.period_s ?? 10}
                            onChange={(e) =>
                              upd({ period_s: Number(e.target.value) as LimitRule["period_s"] })
                            }
                          >
                            {[1, 10, 60, 300, 3600].map((p) => (
                              <option key={p} value={p}>
                                {p === 3600
                                  ? t("hour")
                                  : p >= 60
                                    ? t("{n} min", { n: p / 60 })
                                    : t("{n} s", { n: p })}
                              </option>
                            ))}
                          </select>
                        </label>
                      </>
                    )}
                    <details className="md:col-span-2">
                      <summary className="cursor-pointer text-sm font-medium">
                        {t("Advanced")}
                      </summary>
                      <div className="grid gap-3 md:grid-cols-2">
                        <label>
                          <span className="mb-1.5 block text-sm font-medium">
                            {t("Tracked keys")}
                          </span>
                          <input
                            aria-label={`Rule ${i + 1} table size`}
                            type="number"
                            min={1}
                            className={INPUT}
                            disabled={dis}
                            value={r.table_size ?? 10000}
                            onChange={(e) => upd({ table_size: Number(e.target.value) })}
                          />
                        </label>
                        <label>
                          <span className="mb-1.5 block text-sm font-medium">
                            {t("Entry lifetime, seconds (optional)")}
                          </span>
                          <input
                            aria-label={`Rule ${i + 1} entry ttl`}
                            type="number"
                            min={1}
                            max={86400}
                            className={INPUT}
                            disabled={dis}
                            value={r.entry_ttl_s ?? ""}
                            onChange={(e) =>
                              upd({ entry_ttl_s: e.target.value ? Number(e.target.value) : null })
                            }
                          />
                        </label>
                      </div>
                    </details>
                  </div>
                  <div className="grid gap-3 md:grid-cols-3">
                    <label>
                      <span className="mb-1.5 block text-sm font-medium">
                        {t("Over the limit")}
                      </span>
                      <select
                        aria-label={`Rule ${i + 1} action`}
                        className={INPUT}
                        disabled={dis}
                        value={r.action ?? "deny"}
                        onChange={(e) => upd({ action: e.target.value as LimitRule["action"] })}
                      >
                        <option value="deny">{t("Answer with an error")}</option>
                        <option value="tarpit">{t("Hold, then answer 500")}</option>
                        <option value="silent_drop">{t("Drop silently")}</option>
                      </select>
                    </label>
                    {(r.action ?? "deny") === "deny" && (
                      <label>
                        <span className="mb-1.5 block text-sm font-medium">{t("Status")}</span>
                        <select
                          aria-label={`Rule ${i + 1} deny status`}
                          className={INPUT}
                          disabled={dis}
                          value={r.deny_status ?? 429}
                          onChange={(e) =>
                            upd({ deny_status: Number(e.target.value) as LimitRule["deny_status"] })
                          }
                        >
                          {[403, 429, 503].map((c) => (
                            <option key={c} value={c}>
                              {c}
                            </option>
                          ))}
                        </select>
                      </label>
                    )}
                    {r.action === "tarpit" && (
                      <label>
                        <span className="mb-1.5 block text-sm font-medium">
                          {t("Hold for (s)")}
                        </span>
                        <input
                          aria-label={`Rule ${i + 1} tarpit seconds`}
                          type="number"
                          min={1}
                          className={INPUT}
                          disabled={dis}
                          value={r.tarpit_s ?? 5}
                          onChange={(e) => upd({ tarpit_s: Number(e.target.value) })}
                        />
                      </label>
                    )}
                  </div>
                  <div>
                    <div className="mb-1.5 text-sm font-medium">
                      {t("Count requests that match")}
                    </div>
                    <MatchEditor
                      id={`Rule ${i + 1}`}
                      disabled={dis}
                      match={r.match ?? emptyMatch()}
                      onChange={(m) => upd({ match: m })}
                    />
                  </div>
                </div>
              );
            })}
            <Button
              variant="outline"
              size="sm"
              disabled={dis}
              onClick={() => setLimits([...limits, newLimitRule()])}
            >
              {t("Add rule")}
            </Button>
          </section>
        )}

        {kind === "error_pages" && (
          <section className="space-y-3">
            <p className="text-xs text-muted-foreground">
              {t(
                "Pages for the errors HAProxy generates itself (no server available, timeouts). One per status.",
              )}
            </p>
            {PAGE_STATUSES.map((s) => {
              const p = pages[s];
              return (
                <div key={s} className="space-y-2 rounded-lg border p-3 text-sm">
                  <label className="flex items-center gap-2 font-medium">
                    <input
                      type="checkbox"
                      disabled={dis}
                      aria-label={`Custom page for ${s}`}
                      checked={!!p}
                      onChange={(e) => {
                        const next = { ...pages };
                        if (e.target.checked)
                          next[s] = { status: s, content_type: "text/html", body: "" };
                        else delete next[s];
                        setPages(next);
                      }}
                    />
                    {s}
                  </label>
                  {p && (
                    <div className="space-y-2">
                      <select
                        aria-label={`Page ${s} content type`}
                        className={cn(INPUT, "w-48")}
                        disabled={dis}
                        value={p.content_type ?? "text/plain"}
                        onChange={(e) =>
                          setPages({
                            ...pages,
                            [s]: {
                              ...p,
                              content_type: e.target.value as ErrorPage["content_type"],
                            },
                          })
                        }
                      >
                        {TYPES.map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                      <textarea
                        aria-label={`Page ${s} body`}
                        className={cn(INPUT, "h-28 py-2 font-mono text-xs")}
                        disabled={dis}
                        value={p.body}
                        onChange={(e) =>
                          setPages({ ...pages, [s]: { ...p, body: e.target.value } })
                        }
                      />
                    </div>
                  )}
                </div>
              );
            })}
          </section>
        )}
      </div>
      <div className="flex items-center gap-2 border-t px-5 py-3">
        {policy && (
          <Button
            variant="ghost"
            disabled={dis || usedBy.length > 0}
            onClick={() => setConfirmDelete(true)}
          >
            {t("Delete")}
          </Button>
        )}
        <Button variant="ghost" className="ml-auto" onClick={onClose}>
          {t("Cancel")}
        </Button>
        <Button disabled={dis || !name || save.isPending} onClick={() => void submit()}>
          {t("Save to draft")}
        </Button>
      </div>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete {name}?", { name: policy?.name ?? "" })}
        confirmLabel={t("Delete")}
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
          {t(
            "The policy is removed in your draft. Nothing goes live until the change set is applied.",
          )}
        </p>
      </ConfirmDialog>
    </section>
  );
}
