import { createFileRoute } from "@tanstack/react-router";
import { NotBuilt } from "@/components/sy/NotBuilt";

export const Route = createFileRoute("/t/$tenant/_shell/virtual-services/$id")({
  component: () => <NotBuilt name="Virtual service" />,
});
