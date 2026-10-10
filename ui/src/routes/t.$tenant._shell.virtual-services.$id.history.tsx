import { createFileRoute } from "@tanstack/react-router";
import { VsHistory } from "@/features/vs/VsHistory";

function Page() {
  const { tenant, id } = Route.useParams();
  return <VsHistory tenant={tenant} id={id} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/virtual-services/$id/history")({
  component: Page,
});
