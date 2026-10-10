/** Writing into the change set: every edit is an op in the caller's draft, opened on first edit. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { useMe } from "@/auth/session";
import { useDraft } from "./draft";

/** "Draft by <first name>, <HH:MM>" (handoff copy). */
export function defaultDraftName(fullName: string, at: Date = new Date()): string {
  const first = fullName.trim().split(/\s+/)[0] || fullName;
  const hh = String(at.getHours()).padStart(2, "0");
  const mm = String(at.getMinutes()).padStart(2, "0");
  return `Draft by ${first}, ${hh}:${mm}`;
}

/**
 * Returns `ensure()`, which resolves to the id of the open draft, creating one named
 * "Draft by <first name>, <HH:MM>" when there is none. Refreshes the bar and every tenant query
 * afterwards so lists switch to the draft overlay.
 */
export function useEnsureDraft(tenant: string) {
  const { draft } = useDraft(tenant);
  const { data: me } = useMe();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      if (draft) return draft.id;
      const created = unwrap(
        await api.POST("/api/v1/tenants/{t}/change-sets", {
          params: { path: { t: tenant } },
          body: { summary: defaultDraftName(me?.name ?? "") },
        }),
      );
      await qc.invalidateQueries({ queryKey: ["t", tenant, "change-sets"] });
      return created.id;
    },
  });
}

/** After an op lands, the bar, lists and drafted reads must refetch. */
export function useAfterDraftWrite(tenant: string) {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["t", tenant] });
}
