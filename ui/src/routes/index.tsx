import { createFileRoute, Navigate } from "@tanstack/react-router";
import { useMe, useTenants } from "@/auth/session";
import { ApiError } from "@/api/client";
import { Skeleton } from "@/components/ui/skeleton";

/** `/` sends you to your first tenant, or to the login page when signed out. */
function Landing() {
  const me = useMe();
  const tenants = useTenants();
  if (me.error instanceof ApiError && me.error.status === 401) return <Navigate to="/login" />;
  const first = tenants.data?.[0];
  if (first) return <Navigate to="/t/$tenant" params={{ tenant: first.name }} />;
  return <Skeleton className="m-8 h-10 w-64" />;
}

export const Route = createFileRoute("/")({ component: Landing });
