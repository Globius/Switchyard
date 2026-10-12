// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute, Outlet, useLocation, useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";
import { ApiError } from "@/api/client";
import { useMe } from "@/auth/session";
import { Skeleton } from "@/components/ui/skeleton";

/** Every tenant page needs a session; a 401 sends you to the login page, once. */
function TenantGuard() {
  const me = useMe();
  const navigate = useNavigate();
  const here = useLocation({ select: (l) => l.pathname + (l.searchStr ?? "") });
  const err = me.error instanceof ApiError ? me.error : null;
  const unauthorized = err?.status === 401;
  const ended = unauthorized && err?.problem?.code === "session_expired";
  useEffect(() => {
    if (unauthorized)
      void navigate({
        to: "/login",
        search: { next: here, error: ended ? "session_ended" : undefined },
        replace: true,
      });
    // `here` is read once at the moment of the 401; later changes must not re-fire this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unauthorized, ended]);
  if (!me.data) return <Skeleton className="m-8 h-10 w-64" />;
  return <Outlet />;
}

export const Route = createFileRoute("/t/$tenant")({ component: TenantGuard });
