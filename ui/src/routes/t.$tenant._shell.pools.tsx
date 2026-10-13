// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute } from "@tanstack/react-router";
import { Pools } from "@/features/pools/Pools";

/** The name and label filters live in the URL so every view is linkable. */
function Page() {
  const { tenant } = Route.useParams();
  const { q, label } = Route.useSearch();
  return <Pools tenant={tenant} q={q} label={label} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/pools")({
  validateSearch: (s: Record<string, unknown>): { q?: string; label?: string } => ({
    q: typeof s.q === "string" && s.q ? s.q : undefined,
    label: typeof s.label === "string" && s.label ? s.label : undefined,
  }),
  component: Page,
});
