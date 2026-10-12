import { createFileRoute } from "@tanstack/react-router";
import { Alerts } from "@/features/alerts/Alerts";

function Page() {
  const { tenant } = Route.useParams();
  return <Alerts tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/alerts")({ component: Page });
