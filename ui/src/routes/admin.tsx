import { createFileRoute } from "@tanstack/react-router";
import { AdminShell } from "@/features/admin/AdminShell";

export const Route = createFileRoute("/admin")({ component: AdminShell });
