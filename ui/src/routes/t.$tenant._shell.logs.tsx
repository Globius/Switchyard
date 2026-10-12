import { createFileRoute } from "@tanstack/react-router";
import { RequestLogs } from "@/features/logs/RequestLogs";

function Page() {
  const { tenant } = Route.useParams();
  return <RequestLogs tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/logs")({ component: Page });
