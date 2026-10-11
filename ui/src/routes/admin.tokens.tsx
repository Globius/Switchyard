import { createFileRoute } from "@tanstack/react-router";
import { SystemTokens } from "@/features/admin/SystemTokens";

export const Route = createFileRoute("/admin/tokens")({ component: SystemTokens });
