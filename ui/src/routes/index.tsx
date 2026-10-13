// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useEffect } from "react";
import { useMe, useTenants } from "@/auth/session";
import { ApiError } from "@/api/client";
import { Skeleton } from "@/components/ui/skeleton";

/** `/` sends you to your first tenant, or to the login page when signed out. */
function Landing() {
  const me = useMe();
  const tenants = useTenants();
  const navigate = useNavigate();
  const unauthorized = me.error instanceof ApiError && me.error.status === 401;
  const first = tenants.data?.[0]?.name;
  useEffect(() => {
    if (unauthorized) void navigate({ to: "/login", replace: true });
    else if (first) void navigate({ to: "/t/$tenant", params: { tenant: first }, replace: true });
  }, [unauthorized, first, navigate]);
  return <Skeleton className="m-8 h-10 w-64" />;
}

export const Route = createFileRoute("/")({ component: Landing });
