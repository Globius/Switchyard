import { createFileRoute } from "@tanstack/react-router";
import { NotBuilt } from "@/components/sy/NotBuilt";

// Replaced by the Login screen in its own MR.
export const Route = createFileRoute("/login")({
  component: () => <NotBuilt name="Login" />,
});
