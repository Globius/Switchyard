import { createFileRoute } from "@tanstack/react-router";
import { Home } from "@/features/home/Home";

function Page() {
  const { tenant } = Route.useParams();
  return <Home tenant={tenant} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/")({ component: Page });
