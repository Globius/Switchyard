import { createFileRoute } from "@tanstack/react-router";
import { NotBuilt } from "@/components/sy/NotBuilt";

// Tenant home is U2.
export const Route = createFileRoute("/t/$tenant/_shell/")({
  component: () => <NotBuilt name="Home" />,
});
