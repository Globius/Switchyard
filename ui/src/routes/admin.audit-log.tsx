import { createFileRoute } from "@tanstack/react-router";
import { NotBuilt } from "@/components/sy/NotBuilt";

export const Route = createFileRoute("/admin/audit-log")({
  component: () => <NotBuilt name="Audit log" />,
});
