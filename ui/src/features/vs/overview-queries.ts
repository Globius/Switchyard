// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Extra reads of the Overview's side cards: access log and policy sets. */
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";

export { useVsRendered } from "./detail-queries";

/** Recent access log lines; answers `available: false` with a reason when no log backend exists. */
export function useVsLogs(tenant: string, id: string) {
  return useQuery({
    queryKey: ["t", tenant, "virtual-services", id, "logs"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/virtual-services/{object_id}/logs", {
          params: { path: { t: tenant, object_id: id }, query: { limit: 8 } },
        }),
      ),
  });
}

export function usePolicySets(tenant: string) {
  return useQuery({
    queryKey: ["t", tenant, "policy-sets"],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/policy-sets", {
          params: { path: { t: tenant }, query: { limit: 200 } },
        }),
      ),
  });
}
