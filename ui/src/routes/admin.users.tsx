import { createFileRoute } from "@tanstack/react-router";
import { Users } from "@/features/admin/Users";

export const Route = createFileRoute("/admin/users")({ component: Users });
