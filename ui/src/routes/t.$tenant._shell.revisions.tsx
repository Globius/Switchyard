import { createFileRoute } from "@tanstack/react-router";
import { Revisions } from "@/features/revisions/Revisions";

function Page() {
  const { tenant } = Route.useParams();
  return <Revisions tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/revisions")({ component: Page });
