import { createFileRoute } from "@tanstack/react-router";
import { Monitors } from "@/features/monitors/Monitors";

function Page() {
  const { tenant } = Route.useParams();
  return <Monitors tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/health-monitors")({ component: Page });
