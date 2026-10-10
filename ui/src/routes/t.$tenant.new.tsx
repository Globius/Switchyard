import { createFileRoute } from "@tanstack/react-router";
import { NotBuilt } from "@/components/sy/NotBuilt";

// Full-screen create wizard (no shell).
export const Route = createFileRoute("/t/$tenant/new")({
  component: () => <NotBuilt name="New virtual service" />,
});
