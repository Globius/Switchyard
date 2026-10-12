// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Writing into the change set: every edit is an op in the caller's draft, opened on first edit. */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { useMe } from "@/auth/session";
import { draftOwnerName } from "./author";
import { useDraft } from "./draft";

/** "Draft by <name>, <HH:MM>" (handoff copy); a string is treated as a person's full name. */
export function defaultDraftName(
  who: string | { name: string; auth?: string; break_glass?: boolean },
  at: Date = new Date(),
): string {
  const name = draftOwnerName(typeof who === "string" ? { name: who } : who);
  const hh = String(at.getHours()).padStart(2, "0");
  const mm = String(at.getMinutes()).padStart(2, "0");
  return `Draft by ${name}, ${hh}:${mm}`;
}

/**
 * Returns `ensure()`, which resolves to the id of the open draft, creating one named
 * "Draft by <first name>, <HH:MM>" when there is none. It does not refresh queries: callers add
 * their op first and then invalidate (see {@link useAfterDraftWrite}), otherwise a read of the
 * new draft races the write and shows the state before the edit.
 */
export function useEnsureDraft(tenant: string) {
  const { draft } = useDraft(tenant);
  const { data: me } = useMe();
  return useMutation({
    mutationFn: async () => {
      if (draft) return draft.id;
      const created = unwrap(
        await api.POST("/api/v1/tenants/{t}/change-sets", {
          params: { path: { t: tenant } },
          body: { summary: defaultDraftName(me ?? "") },
        }),
      );
      return created.id;
    },
  });
}

/** After an op lands, the bar, lists and drafted reads must refetch. */
export function useAfterDraftWrite(tenant: string) {
  const qc = useQueryClient();
  return () => qc.invalidateQueries({ queryKey: ["t", tenant] });
}
