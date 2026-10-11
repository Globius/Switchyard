import { createFileRoute } from "@tanstack/react-router";
import { Tenants } from "@/features/admin/Tenants";

export const Route = createFileRoute("/admin/tenants")({ component: Tenants });
