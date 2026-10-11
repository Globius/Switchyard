/** Validator findings of the open draft that concern one virtual service. */
import { ApiError } from "@/api/client";
import type { Finding } from "@/components/sy/Findings";
import { useDiff } from "@/features/review/queries";
import { useDraft } from "@/features/shell/draft";

/** Problems whose path is under `virtual_services.<name>.`; paths are kept exactly as sent. */
export function findingsFor(
  problems: { path: string; message: string; code?: string }[],
  name: string | undefined,
): Finding[] {
  if (!name) return [];
  return problems.filter(
    (p) => p.path.startsWith(`virtual_services.${name}.`) || p.path === `virtual_services.${name}`,
  );
}

/** Findings from a failed read of the drafted object (422 `errors[]`). */
export function findingsOfError(error: unknown): Finding[] {
  if (error instanceof ApiError && error.status === 422)
    return (error.problem?.errors ?? []).map((e) => ({
      path: e.path,
      message: e.message,
      code: e.code,
    }));
  return [];
}

/** The service's findings from the draft diff, or none when there is no draft. */
export function useServiceFindings(tenant: string, name: string | undefined) {
  const { draft } = useDraft(tenant);
  const diff = useDiff(tenant, draft?.id ?? "", !!draft);
  return { draft, findings: draft ? findingsFor(diff.data?.problems ?? [], name) : [] };
}
