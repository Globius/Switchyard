import { createFileRoute } from "@tanstack/react-router";
import { NotBuilt } from "@/components/sy/NotBuilt";

// Replaced by the Settings tab in the next MR.
export const Route = createFileRoute("/t/$tenant/_shell/virtual-services/$id/settings")({
  component: () => <NotBuilt name="Settings" />,
});
