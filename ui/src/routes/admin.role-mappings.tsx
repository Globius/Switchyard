import { createFileRoute } from "@tanstack/react-router";
import { NotBuilt } from "@/components/sy/NotBuilt";

export const Route = createFileRoute("/admin/role-mappings")({
  component: () => <NotBuilt name="Role mappings" />,
});
