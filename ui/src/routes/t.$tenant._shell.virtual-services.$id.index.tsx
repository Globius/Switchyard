import { createFileRoute } from "@tanstack/react-router";
import { VsOverview } from "@/features/vs/VsOverview";

function Page() {
  const { tenant, id } = Route.useParams();
  return <VsOverview tenant={tenant} id={id} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/virtual-services/$id/")({
  component: Page,
});
