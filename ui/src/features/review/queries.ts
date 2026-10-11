import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, rawGet, unwrap } from "@/api/client";
import { diffObject, type FieldChange } from "@/lib/snapshot-diff";
import { TYPE_PATH } from "./model";

export function useChangeSetRead(tenant: string, cs: string) {
  return useQuery({
    queryKey: ["t", tenant, "change-sets", cs],
    // While an apply runs the state changes underneath us (applying, then failed or applied).
    refetchInterval: (q) => (q.state.data?.state === "applying" ? 1500 : false),
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/change-sets/{cs}", {
          params: { path: { t: tenant, cs } },
        }),
      ),
  });
}

/** The rendered diff, problems, warnings and impact of a change set; nothing is saved. */
export function useDiff(tenant: string, cs: string, enabled = true) {
  return useQuery({
    queryKey: ["t", tenant, "change-sets", cs, "diff"],
    enabled,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/change-sets/{cs}/diff", {
          params: { path: { t: tenant, cs } },
        }),
      ),
  });
}

export function useTenantSettings(tenant: string) {
  return useQuery({
    queryKey: ["tenants"],
    queryFn: async () => unwrap(await api.GET("/api/v1/tenants")),
    select: (all) => all.find((x) => x.name === tenant || x.id === tenant),
  });
}

/** The latest apply job of a change set (the jobs list has no filter by change set). */
export function useApplyJob(tenant: string, cs: string, jobId: string | null, poll: boolean) {
  return useQuery({
    queryKey: ["t", tenant, "jobs", { cs, jobId }],
    refetchInterval: poll ? 1500 : false,
    queryFn: async () => {
      if (jobId)
        return unwrap(
          await api.GET("/api/v1/tenants/{t}/jobs/{job_id}", {
            params: { path: { t: tenant, job_id: jobId } },
          }),
        );
      const list = unwrap(
        await api.GET("/api/v1/tenants/{t}/jobs", {
          params: { path: { t: tenant }, query: { kind: "apply_change_set", limit: 50 } },
        }),
      );
      return list.items.find((j) => j.change_set_id === cs) ?? null;
    },
  });
}

/** Live and drafted read of one object, for the before and after values. */
export function useObjectChange(
  tenant: string,
  type: string,
  id: string,
  cs: string,
  status: string,
) {
  return useQuery({
    queryKey: ["t", tenant, "review", cs, type, id],
    queryFn: async (): Promise<{ fields: FieldChange[]; draft?: Record<string, unknown> }> => {
      const base = `/api/v1/tenants/${encodeURIComponent(tenant)}/${TYPE_PATH[type]}/${id}`;
      if (status === "created") {
        const draft = await rawGet<Record<string, unknown>>(`${base}?change_set=${cs}`);
        return { fields: diffObject(undefined, draft), draft };
      }
      if (status === "deleted") return { fields: [] };
      const [live, draft] = await Promise.all([
        rawGet<Record<string, unknown>>(base),
        rawGet<Record<string, unknown>>(`${base}?change_set=${cs}`),
      ]);
      return { fields: diffObject(live, draft), draft };
    },
  });
}

export function useChangeSetActions(tenant: string, cs: string) {
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: ["t", tenant] });
  const path = { params: { path: { t: tenant, cs } } };
  return {
    apply: useMutation({
      mutationFn: async (reason: string | null) =>
        unwrap(
          await api.POST("/api/v1/tenants/{t}/change-sets/{cs}/apply", {
            ...path,
            body: reason ? { break_glass_reason: reason } : {},
          }),
        ),
      onSuccess: refresh,
    }),
    requestApproval: useMutation({
      mutationFn: async () =>
        unwrap(await api.POST("/api/v1/tenants/{t}/change-sets/{cs}/request-approval", path)),
      onSuccess: refresh,
    }),
    approve: useMutation({
      mutationFn: async () =>
        unwrap(await api.POST("/api/v1/tenants/{t}/change-sets/{cs}/approve", path)),
      onSuccess: refresh,
    }),
    reject: useMutation({
      mutationFn: async (comment: string) =>
        unwrap(
          await api.POST("/api/v1/tenants/{t}/change-sets/{cs}/reject", {
            ...path,
            body: { comment },
          }),
        ),
      onSuccess: refresh,
    }),
  };
}
