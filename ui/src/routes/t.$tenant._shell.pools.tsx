import { createFileRoute } from "@tanstack/react-router";
import { Pools } from "@/features/pools/Pools";

/** The name filter lives in the URL so every view is linkable. */
function Page() {
  const { tenant } = Route.useParams();
  const { q } = Route.useSearch();
  return <Pools tenant={tenant} q={q} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/pools")({
  validateSearch: (s: Record<string, unknown>): { q?: string } => ({
    q: typeof s.q === "string" && s.q ? s.q : undefined,
  }),
  component: Page,
});
