import { createFileRoute, Navigate, Outlet } from "@tanstack/react-router";
import { ApiError } from "@/api/client";
import { useMe } from "@/auth/session";
import { Skeleton } from "@/components/ui/skeleton";

/** Every tenant page needs a session; a 401 sends you to the login page. */
function TenantGuard() {
  const me = useMe();
  if (me.error instanceof ApiError && me.error.status === 401) return <Navigate to="/login" />;
  if (!me.data) return <Skeleton className="m-8 h-10 w-64" />;
  return <Outlet />;
}

export const Route = createFileRoute("/t/$tenant")({ component: TenantGuard });
