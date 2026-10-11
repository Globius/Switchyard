import { createFileRoute } from "@tanstack/react-router";
import { VsSettings } from "@/features/vs/VsSettings";

function Page() {
  const { tenant, id } = Route.useParams();
  return <VsSettings tenant={tenant} id={id} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/virtual-services/$id/settings")({
  component: Page,
});
