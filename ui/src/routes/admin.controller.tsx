import { createFileRoute } from "@tanstack/react-router";
import { AdminController } from "@/features/admin/AdminController";

export const Route = createFileRoute("/admin/controller")({ component: AdminController });
