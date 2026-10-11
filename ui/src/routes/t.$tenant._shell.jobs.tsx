import { createFileRoute } from "@tanstack/react-router";
import { Jobs } from "@/features/jobs/Jobs";

function Page() {
  const { tenant } = Route.useParams();
  return <Jobs tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/jobs")({ component: Page });
