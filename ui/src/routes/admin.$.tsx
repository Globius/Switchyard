import { createFileRoute } from "@tanstack/react-router";
import { NotBuilt } from "@/components/sy/NotBuilt";

// Admin screens are U4.
export const Route = createFileRoute("/admin/$")({ component: () => <NotBuilt name="Admin" /> });
