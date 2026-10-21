// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { VsSettings } from "@/features/vs/VsSettings";

function Page() {
  const { tenant, id } = Route.useParams();
  const { field, add } = Route.useSearch();
  return <VsSettings tenant={tenant} id={id} focus={field} addRoute={add === "route"} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/virtual-services/$id/settings")({
  validateSearch: (s: Record<string, unknown>): { field?: string; add?: "route" } => ({
    field: typeof s.field === "string" && s.field ? s.field : undefined,
    add: s.add === "route" ? "route" : undefined,
  }),
  component: Page,
});
