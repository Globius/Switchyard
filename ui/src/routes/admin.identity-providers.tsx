import { createFileRoute } from "@tanstack/react-router";
import { IdentityProviders } from "@/features/admin/IdentityProviders";

export const Route = createFileRoute("/admin/identity-providers")({ component: IdentityProviders });
