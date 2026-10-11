import { createFileRoute } from "@tanstack/react-router";
import { SystemSettings } from "@/features/admin/SystemSettings";

export const Route = createFileRoute("/admin/settings")({ component: SystemSettings });
