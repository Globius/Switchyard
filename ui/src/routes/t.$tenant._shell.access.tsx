import { createFileRoute } from "@tanstack/react-router";
import { Access } from "@/features/access/Access";

function Page() {
  const { tenant } = Route.useParams();
  return <Access tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/access")({ component: Page });
