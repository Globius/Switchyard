import { createFileRoute } from "@tanstack/react-router";
import { Portal } from "@/features/portal/Portal";

function Page() {
  const { tenant } = Route.useParams();
  return <Portal tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/portal")({ component: Page });
