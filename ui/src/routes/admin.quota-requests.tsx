import { createFileRoute } from "@tanstack/react-router";
import { QuotaQueue } from "@/features/portal/QuotaQueue";

export const Route = createFileRoute("/admin/quota-requests")({ component: QuotaQueue });
