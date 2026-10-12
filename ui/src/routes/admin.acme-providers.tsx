import { createFileRoute } from "@tanstack/react-router";
import { AcmeProviders } from "@/features/admin/AcmeProviders";

export const Route = createFileRoute("/admin/acme-providers")({ component: AcmeProviders });
