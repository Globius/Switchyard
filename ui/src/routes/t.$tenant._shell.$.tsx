import { createFileRoute } from "@tanstack/react-router";
import { NotBuilt } from "@/components/sy/NotBuilt";

/** Sections that are not built yet land here; each gets its own route file as it ships. */
function Section() {
  const { _splat } = Route.useParams();
  return <NotBuilt name={_splat ?? ""} />;
}

export const Route = createFileRoute("/t/$tenant/_shell/$")({ component: Section });
