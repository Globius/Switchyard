import { createFileRoute } from "@tanstack/react-router";
import { Policies } from "@/features/policies/Policies";

function Page() {
  const { tenant } = Route.useParams();
  return <Policies tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/policies")({ component: Page });
