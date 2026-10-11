/** Open draft of the caller in a tenant, for the change-set bar and the draft overlay reads. */
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useMe } from "@/auth/session";

export type ChangeSetSummary = components["schemas"]["ChangeSetSummary"];

/**
 * The caller's open draft (state `draft` or `pending_approval`), or undefined.
 *
 * The API's author is `user:<provider>:<subject>` for sessions and `token:<name>` for tokens, and
 * `/me` does not return it, so this matches the name, the email or a trailing `:<name>`. That is
 * a guess for providers whose subject differs from the user name; `/me` should return the author
 * string (raised with backend). Other people's drafts are listed on the Change sets page, not here.
 */
export function pickOwnDraft(
  items: ChangeSetSummary[],
  me: { name: string; email?: string | null } | undefined,
): ChangeSetSummary | undefined {
  if (!me) return undefined;
  return items.find(
    (c) =>
      (c.state === "draft" || c.state === "pending_approval") &&
      (c.author === me.name ||
        c.author.endsWith(`:${me.name}`) ||
        (me.email != null && c.author === me.email)),
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
