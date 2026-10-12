import { createFileRoute } from "@tanstack/react-router";
import { Gslb } from "@/features/gslb/Gslb";

function Page() {
  const { tenant } = Route.useParams();
  return <Gslb tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/gslb")({ component: Page });
