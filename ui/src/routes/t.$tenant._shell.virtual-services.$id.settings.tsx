import { createFileRoute } from "@tanstack/react-router";
import { VsSettings } from "@/features/vs/VsSettings";

function Page() {
  const { tenant, id } = Route.useParams();
  const { field } = Route.useSearch();
  return <VsSettings tenant={tenant} id={id} focus={field} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/virtual-services/$id/settings")({
  validateSearch: (s: Record<string, unknown>): { field?: string } => ({
    field: typeof s.field === "string" && s.field ? s.field : undefined,
  }),
  component: Page,
});
