import { createFileRoute } from "@tanstack/react-router";
import { Kubernetes } from "@/features/kubernetes/Kubernetes";

function Page() {
  const { tenant } = Route.useParams();
  return <Kubernetes tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/kubernetes")({ component: Page });
