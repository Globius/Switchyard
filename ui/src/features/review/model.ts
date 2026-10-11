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
  job: Pick<Job, "state" | "retrying" | "result" | "ok"> | undefined,
): Phase {
  if (state === "applied") return "applied";
  if (state === "discarded") return "discarded";
  if (state === "pending_approval") return "pending_approval";
  if (job) {
    if (job.state === "dead") return "gave_up";
    if (job.state === "queued" && job.retrying) return "retrying";
    if (job.state === "queued" || job.state === "running") return "applying";
    const ok = job.ok ?? (job.result as { ok?: boolean } | null | undefined)?.ok;
    if (job.state === "succeeded" && ok === false) return "reverted";
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
