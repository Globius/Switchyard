/** Queries behind the virtual services list. Keys mirror API paths so an apply invalidates by prefix. */
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";

export interface VsFilters {
  q?: string;
  sort?: "name" | "-name";
  se_group?: string;
  protocol?: "http" | "tcp" | "udp";
  enabled?: boolean;
}

/** Cursor-paged list; `changeSet` overlays the open draft (`?change_set=`). */
export function useVirtualServices(tenant: string, filters: VsFilters, changeSet?: string) {
  return useInfiniteQuery({
    queryKey: ["t", tenant, "virtual-services", { ...filters, changeSet }],
    queryFn: async ({ pageParam }) =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/virtual-services", {
          params: {
            path: { t: tenant },
            query: { ...filters, change_set: changeSet, cursor: pageParam, limit: 50 },
          },
        }),
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
}

/** Health and member counts per service, from engine status. */
export function useVsStatus(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "status", "virtual-services"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/status/virtual-services", {
          params: { path: { t: tenant } },
        }),
      ),
  });
}

/** RPS, p95 and 5xx; answers `available: false` with a reason when no metrics backend exists. */
export function useVsMetrics(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "metrics", "virtual-services"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/metrics/virtual-services", {
          params: { path: { t: tenant } },
        }),
      ),
  });
}

/** All SE groups (names for the SE group column and filter). */
export function useSeGroups(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "se-groups"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/se-groups", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
}

/** All certificates (expiry column). */
export function useCertificates(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "certificates"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/certificates", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
}
