import { createFileRoute } from "@tanstack/react-router";
import { Login, type LoginError } from "@/features/login/Login";

const CODES = ["provider_error", "expired", "no_access", "disabled", "session_ended"] as const;

function Page() {
  const { error, next } = Route.useSearch();
  return <Login error={error} next={next} />;
}

export const Route = createFileRoute("/login")({
  validateSearch: (s: Record<string, unknown>): { error?: LoginError; next?: string } => ({
    error: CODES.find((c) => c === s.error),
    next: typeof s.next === "string" ? s.next : undefined,
  }),
  component: Page,
});
