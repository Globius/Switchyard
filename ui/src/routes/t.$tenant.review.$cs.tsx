import { createFileRoute } from "@tanstack/react-router";
import { NotBuilt } from "@/components/sy/NotBuilt";

// Full-screen review and apply (no shell).
export const Route = createFileRoute("/t/$tenant/review/$cs")({
  component: () => <NotBuilt name="Review and apply" />,
});
