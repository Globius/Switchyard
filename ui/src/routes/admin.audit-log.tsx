import { createFileRoute } from "@tanstack/react-router";
import { AuditLog } from "@/features/admin/AuditLog";

export const Route = createFileRoute("/admin/audit-log")({ component: AuditLog });
