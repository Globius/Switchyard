import { createFileRoute } from "@tanstack/react-router";
import { Pools } from "@/features/pools/Pools";

function Page() {
  const { tenant } = Route.useParams();
  return <Pools tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/pools")({ component: Page });
