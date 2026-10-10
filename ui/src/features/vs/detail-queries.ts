/** Queries and mutations for one virtual service. Keys mirror API paths. */
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";

export type PoolRead = components["schemas"]["PoolRead"];
export type AdminState = components["schemas"]["AdminState"];

/** One virtual service; `changeSet` overlays the open draft. */
export function useVs(tenant: string, id: string, changeSet?: string) {
  return useQuery({
    queryKey: ["t", tenant, "virtual-services", id, { changeSet }],
    // Opening a draft changes the key; keep showing the form instead of flashing a skeleton.
    placeholderData: keepPreviousData,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/virtual-services/{object_id}", {
          params: { path: { t: tenant, object_id: id }, query: { change_set: changeSet } },
        }),
      ),
  });
}

/** Rendered fragments this object produces (live, or with the draft applied). */
export function useVsRendered(tenant: string, id: string, changeSet?: string) {
  return useQuery({
    queryKey: ["t", tenant, "virtual-services", id, "rendered", { changeSet }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/virtual-services/{object_id}/rendered", {
          params: { path: { t: tenant, object_id: id }, query: { change_set: changeSet } },
        }),
      ),
  });
}

/** Audit events for one object (History tab). */
export function useObjectAudit(tenant: string, objectRef: string) {
  return useQuery({
    queryKey: ["t", tenant, "audit-events", { object: objectRef }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/audit-events", {
          params: { path: { t: tenant }, query: { object: objectRef, limit: 50 } },
        }),
      ),
  });
}

/** Revisions that touched one object (History tab). */
export function useObjectRevisions(tenant: string, objectRef: string) {
  return useQuery({
    queryKey: ["t", tenant, "revisions", { object: objectRef }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/revisions", {
          params: { path: { t: tenant }, query: { object: objectRef, limit: 50 } },
        }),
      ),
  });
}

/** All pools, to resolve route targets and show members. */
export function usePools(tenant: string, changeSet?: string) {
  return useQuery({
    queryKey: ["t", tenant, "pools", { changeSet }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/pools", {
          params: { path: { t: tenant }, query: { limit: 200, change_set: changeSet } },
        }),
      ),
  });
}

/** Runtime member state (drain, enable, disable). Skips the change set; answers per engine. */
export function useSetMemberState(tenant: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { pool: string; member: string; state: AdminState }) =>
      unwrap(
        await api.PUT("/api/v1/tenants/{t}/pools/{pool}/members/{member}/state", {
          params: { path: { t: tenant, pool: v.pool, member: v.member } },
          body: { state: v.state },
        }),
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["t", tenant, "pools"] }),
  });
}
