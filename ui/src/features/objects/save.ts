/**
 * Save create, update and delete ops of one object into the caller's draft (opened on first use).
 * Every object screen saves this way; nothing goes live until the change set is applied.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useEnsureDraft } from "@/features/shell/draft-write";

type ObjectType = components["schemas"]["ObjectType"];

export type Save =
  | { action: "create"; id?: string; body: Record<string, unknown> }
  | { action: "update"; id: string; version: number; body: Record<string, unknown> }
  | { action: "delete"; id: string; version: number };

export function useSaveOp(tenant: string, type: ObjectType) {
  const ensure = useEnsureDraft(tenant);
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (s: Save) => {
      const cs = await ensure.mutateAsync();
      const id = s.action === "create" ? (s.id ?? crypto.randomUUID()) : s.id;
      unwrap(
        await api.POST("/api/v1/tenants/{t}/change-sets/{cs}/ops", {
          params: { path: { t: tenant, cs } },
          body: {
            ops: [
              {
                action: s.action,
                object_type: type,
                object_id: id,
                ...(s.action === "delete" ? {} : { body: s.body }),
                ...(s.action === "create" ? {} : { expected_version: s.version }),
              },
            ],
          },
        }),
      );
      await qc.invalidateQueries({ queryKey: ["t", tenant] });
      return id;
    },
  });
}
