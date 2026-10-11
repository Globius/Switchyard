import { createFileRoute } from "@tanstack/react-router";
import { SeGroups } from "@/features/segroups/SeGroups";

function Page() {
  const { tenant } = Route.useParams();
  return <SeGroups tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/se-groups")({ component: Page });
