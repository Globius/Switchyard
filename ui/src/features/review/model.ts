import { t } from "@/lib/i18n";
/** Pure helpers of the Review and apply screen. */
import type { components } from "@/api/schema";

export type Job = components["schemas"]["Job"];
export type Diff = components["schemas"]["ChangeSetDiff"];
export type ChangeSet = components["schemas"]["ChangeSetRead"];

/** URL segment of each object type. */
export const TYPE_PATH: Record<string, string> = {
  network: "networks",
  vip_pool: "vip-pools",
  se_group: "se-groups",
  virtual_service: "virtual-services",
  pool: "pools",
  health_monitor: "health-monitors",
  certificate: "certificates",
  policy_set: "policy-sets",
  routing_profile: "routing-profiles",
};

export const TYPE_LABEL: Record<string, string> = {
  network: "Network",
  vip_pool: "VIP pool",
  se_group: "SE group",
  virtual_service: "Virtual service",
  pool: "Pool",
  health_monitor: "Health monitor",
  certificate: "Certificate",
  policy_set: "Policy set",
  routing_profile: "Routing profile",
};

/** What the apply panel shows, from the change set and its latest apply job. */
export type Phase =
  | "ready"
  | "pending_approval"
  | "applying"
  | "retrying"
  | "reverted"
  | "gave_up"
  | "failed"
  | "applied"
  | "discarded";

/**
 * The phase of a change set. `succeeded` with `ok: false` is a finished, reverted apply (never
 * retried); `queued` with `retrying` is waiting after an infrastructure error; `dead` gave up.
 *
 * @example
 * phaseOf("draft", { state: "succeeded", result: { ok: false } }) // "reverted"
 */
export function phaseOf(
  state: string,
  job: (Pick<Job, "state" | "retrying" | "result" | "ok"> & { stages?: Job["stages"] }) | undefined,
): Phase {
  if (state === "applied") return "applied";
  if (state === "discarded") return "discarded";
  if (state === "pending_approval") return "pending_approval";
  if (job) {
    if (job.state === "dead") return "gave_up";
    if (job.state === "queued" && job.retrying) return "retrying";
    if (job.state === "queued" || job.state === "running") return "applying";
    const ok = job.ok ?? (job.result as { ok?: boolean } | null | undefined)?.ok;
    if (job.state === "succeeded" && ok === false) {
      // Only a failure after the config reached an engine is a revert; earlier stages just failed.
      const failed = job.stages?.find((x) => x.state === "failed")?.name;
      return failed && !/^(canary|rollout|commit)/.test(failed) ? "failed" : "reverted";
    }
    if (job.state === "failed") return "failed";
  }
  if (state === "applying") return "applying";
  if (state === "failed") return "failed";
  return "ready";
}

/** Short counts line for the header, for example "1 created, 2 modified". */
export function countsLine(objects: { status: string }[]): string {
  const n = (s: string) => objects.filter((o) => o.status === s).length;
  return (["created", "modified", "deleted"] as const)
    .filter((s) => n(s) > 0)
    .map((s) => `${n(s)} ${s}`)
    .join(", ");
}

export interface UnifiedLine {
  kind: "add" | "del" | "hunk" | "ctx";
  text: string;
}

/** Classify the lines of a unified diff so added and removed lines get + and − as well as colour. */
export function parseUnified(diff: string): UnifiedLine[] {
  return diff
    .replace(/\n$/, "")
    .split("\n")
    .filter((l) => !l.startsWith("---") && !l.startsWith("+++"))
    .map((l) =>
      l.startsWith("@@")
        ? { kind: "hunk" as const, text: l }
        : l.startsWith("+")
          ? { kind: "add" as const, text: l.slice(1) }
          : l.startsWith("-")
            ? { kind: "del" as const, text: l.slice(1) }
            : { kind: "ctx" as const, text: l.replace(/^ /, "") },
    );
}

/** True when the draft cannot be applied: blocking validator problems, or no diff yet. */
export function blocked(diff: Diff | undefined): boolean {
  return !diff || diff.problems.length > 0;
}

type Obj = Record<string, unknown>;

/** One-line summary of a created object, for the Objects tab (names resolved through `labels`). */
export function describeCreated(type: string, o: Obj, labels: Record<string, string>): string {
  const name = (id: unknown) => (typeof id === "string" ? (labels[id] ?? id) : "—");
  if (type === "virtual_service") {
    const ports =
      (o.ports as
        | { port: number; tls?: boolean; http2?: boolean; https_redirect_port?: number | null }[]
        | undefined) ?? [];
    const portText = ports
      .map((p) =>
        p.https_redirect_port
          ? `${p.port} → ${p.https_redirect_port}`
          : `${p.port}${p.tls ? " TLS" : ""}${p.http2 ? " h2" : ""}`,
      )
      .join(", ");
    const routes = ((o.routes as unknown[] | undefined) ?? []).length;
    const parts = [
      String(o.protocol ?? "").toUpperCase(),
      name(o.se_group_id),
      `VIP ${o.vip_pool_id ? `from ${name(o.vip_pool_id)}` : (o.vip ?? "—")}${o.vip ? ` (${o.vip})` : ""}`,
      portText,
      routes
        ? `${routes} route${routes === 1 ? "" : "s"}${o.default_pool_id ? ` → ${name(o.default_pool_id)}` : ""}`
        : o.default_pool_id
          ? `→ ${name(o.default_pool_id)}`
          : "",
    ];
    return parts.filter(Boolean).join(" · ");
  }
  if (type === "pool") {
    const m = (o.members as { address: string; port?: number | null }[] | undefined) ?? [];
    return `${String(o.algorithm ?? "")} · ${m.length} member${m.length === 1 ? "" : "s"}${m.length ? ` (${m.map((x) => `${x.address}${x.port ? `:${x.port}` : ""}`).join(", ")})` : ""}`;
  }
  if (type === "health_monitor")
    return `${String(o.type ?? "").toUpperCase()} · every ${o.interval_s} s, timeout ${o.timeout_s} s`;
  if (type === "certificate")
    return `${String(o.source ?? "")} · ${((o.sans as string[] | undefined) ?? []).join(", ")}`;
  if (type === "policy_set") return String(o.kind ?? "");
  return Object.entries(o)
    .filter(
      ([k]) =>
        !["id", "name", "tenant_id", "version", "changed_fields", "draft_status"].includes(k),
    )
    .slice(0, 4)
    .map(([k, v]) => `${k} ${typeof v === "object" ? JSON.stringify(v) : v}`)
    .join(" · ");
}

/** What deleting an object releases, for the Objects tab. */
export function describeDeleted(
  type: string,
  o: Obj | undefined,
  labels: Record<string, string>,
): string {
  if (type === "virtual_service" && o?.vip)
    return `Frees ${o.vip}${typeof o.vip_pool_id === "string" ? ` back to ${labels[o.vip_pool_id] ?? "its VIP pool"}` : ""}`;
  return "Removed when the change set is applied.";
}

/** What to do about a validator problem, when there is a standard answer (currently quotas). */
export function problemHint(p: { code?: string | null; path: string }): string | null {
  if (p.code === "wrong_certificate_purpose")
    return t(
      "A client CA can only verify client certificates, and a server certificate cannot be used as a client CA. Pick a certificate with the right purpose.",
    );
  if (p.code !== "quota_exceeded") return null;
  const type = p.path.replace(/^quota\./, "").replace(/_/g, " ");
  return t(
    "The tenant is at its limit for {type}. Delete something you no longer need, or ask a system-admin to raise the quota.",
    { type },
  );
}
