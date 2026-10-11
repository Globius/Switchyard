import { createFileRoute } from "@tanstack/react-router";
import { NotBuilt } from "@/components/sy/NotBuilt";

export const Route = createFileRoute("/admin/identity-providers")({
  component: () => <NotBuilt name="Identity providers" />,
});
