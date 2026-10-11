import { createFileRoute } from "@tanstack/react-router";
import { RoleMappings } from "@/features/admin/RoleMappings";

export const Route = createFileRoute("/admin/role-mappings")({ component: RoleMappings });
