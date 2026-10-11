import { createFileRoute } from "@tanstack/react-router";
import { Networks } from "@/features/networks/Networks";

function Page() {
  const { tenant } = Route.useParams();
  return <Networks tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/networks")({ component: Page });
