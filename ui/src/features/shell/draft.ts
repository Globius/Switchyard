// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Open draft of the caller in a tenant, for the change-set bar and the draft overlay reads. */
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useMe } from "@/auth/session";

export type ChangeSetSummary = components["schemas"]["ChangeSetSummary"];

/**
 * The caller's open draft (state `draft` or `pending_approval`), or undefined. A draft is yours
 * when its `author` is the `principal` that `GET /me` returns. Other people's drafts are listed
 * on the Change sets page, not here.
 */
export function pickOwnDraft(
  items: ChangeSetSummary[],
  me: { principal?: string } | undefined,
): ChangeSetSummary | undefined {
  if (!me?.principal) return undefined;
  return items.find(
    (c) => (c.state === "draft" || c.state === "pending_approval") && c.author === me.principal,
  );
}

export function useDraft(tenant: string) {
  const { data: me } = useMe();
  const q = useQuery({
    queryKey: ["t", tenant, "change-sets", { open: true }],
    queryFn: async () => {
      const pages = await Promise.all(
        (["draft", "pending_approval"] as const).map(async (state) =>
          unwrap(
            await api.GET("/api/v1/tenants/{t}/change-sets", {
              params: { path: { t: tenant }, query: { state, limit: 100 } },
            }),
          ),
        ),
      );
      return pages.flatMap((p) => p.items);
    },
  });
  return { ...q, draft: pickOwnDraft(q.data ?? [], me) };
}
