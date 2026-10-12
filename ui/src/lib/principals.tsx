// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "@/api/client";
import { principalName } from "@/lib/format";

/**
 * Resolves principal labels (`user:…`, `token:…`) to display names with one batched request.
 *
 * Until the answer arrives, or if it fails, the name derived from the label itself is shown, so a
 * list never waits on the lookup. Pass `tenant = null` on admin screens (system-wide endpoint).
 *
 * @example
 * const name = usePrincipalNames(tenant, rows.map((r) => r.author));
 * name("user:authentik:dana"); // "Dana Reyes" once resolved, "dana" before
 */
export function usePrincipalNames(
  tenant: string | null,
  actors: (string | null | undefined)[],
): (actor: string | null | undefined) => string {
  const labels = [...new Set(actors.filter((a): a is string => !!a))].sort().slice(0, 100);
  const q = useQuery({
    queryKey: ["principals", tenant, labels],
    enabled: labels.length > 0,
    staleTime: 5 * 60_000,
    queryFn: async () =>
      tenant
        ? unwrap(
            await api.GET("/api/v1/tenants/{t}/principals", {
              params: { path: { t: tenant }, query: { actor: labels } },
            }),
          )
        : unwrap(await api.GET("/api/v1/principals", { params: { query: { actor: labels } } })),
  });
  const names = new Map((q.data ?? []).map((p) => [p.actor, p.display_name]));
  return (actor) => (actor ? (names.get(actor) ?? principalName(actor)) : "—");
}

/** One principal's display name, for places that render a single label (history entries). */
export function PrincipalName({
  tenant,
  actor,
}: {
  tenant: string | null;
  actor: string | null | undefined;
}) {
  const name = usePrincipalNames(tenant, [actor]);
  return <>{name(actor)}</>;
}
