// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** The system-admin area: its own top bar and nav, outside any tenant. */
import { Link, Outlet } from "@tanstack/react-router";
import { Signpost } from "lucide-react";
import { useRef } from "react";
import { useMe } from "@/auth/session";
import { Badge } from "@/components/ui/badge";
import { usePendingCount } from "@/features/portal/QuotaQueue";
import { useKeepActiveInView } from "@/features/shell/useNavScroll";
import { UserMenu } from "@/features/shell/UserMenu";
import { t } from "@/lib/i18n";

const NAV = [
  { label: "Tenants", to: "/admin/tenants" },
  { label: "Quota requests", to: "/admin/quota-requests" },
  { label: "Users", to: "/admin/users" },
  { label: "Identity providers", to: "/admin/identity-providers" },
  { label: "Role mappings", to: "/admin/role-mappings" },
  { label: "System tokens", to: "/admin/tokens" },
  { label: "ACME providers", to: "/admin/acme-providers" },
  { label: "Integrations", to: "/admin/integrations" },
  { label: "Controller", to: "/admin/controller" },
  { label: "Upgrades", to: "/admin/upgrades" },
  { label: "System settings", to: "/admin/settings" },
  { label: "Audit log", to: "/admin/audit-log" },
] as const;

/** The waiting count next to "Quota requests", so a request does not sit unseen. */
function WaitingBadge() {
  const n = usePendingCount();
  return n > 0 ? (
    <Badge tone="pending" className="ml-2" aria-label={t("{n} waiting", { n })}>
      {n}
    </Badge>
  ) : null;
}

export function AdminShell() {
  const { data: me, isPending } = useMe();
  const navRef = useRef<HTMLElement>(null);
  useKeepActiveInView(navRef);
  const back = me?.tenants[0]?.name;
  return (
    <div className="flex min-h-screen flex-col">
      <header className="sticky top-0 z-40 flex h-14 items-center gap-3 border-b bg-card px-4">
        <span className="flex items-center gap-2 font-semibold">
          <span className="grid size-7 place-items-center rounded-md bg-primary text-primary-foreground">
            <Signpost className="size-4" aria-hidden />
          </span>
          Switchyard
        </span>
        <Badge tone="pending">{t("System admin")}</Badge>
        {back && (
          <Link
            to="/t/$tenant"
            params={{ tenant: back }}
            className="ml-auto text-sm hover:underline"
          >
            {t("Back to {tenant}", { tenant: back })}
          </Link>
        )}
        <span className={back ? "" : "ml-auto"}>
          <UserMenu />
        </span>
      </header>
      <div className="flex flex-1">
        <nav
          ref={navRef}
          aria-label={t("Admin sections")}
          className="sticky top-14 h-[calc(100dvh-3.5rem)] w-56 shrink-0 self-start overflow-y-auto border-r bg-sidebar p-3 text-sm"
        >
          <div className="px-3 py-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
            {t("Admin")}
          </div>
          {NAV.map((i) => (
            <Link
              key={i.to}
              to={i.to}
              className="block rounded-md px-3 py-2 hover:bg-sidebar-accent"
              activeProps={{ className: "bg-sidebar-accent font-medium" }}
            >
              {t(i.label)}
              {i.to === "/admin/quota-requests" && me?.is_system && <WaitingBadge />}
            </Link>
          ))}
        </nav>
        <main className="flex-1 p-8">
          {!isPending && !me?.is_system ? (
            <div role="alert">
              <h1 className="text-2xl font-semibold">{t("Admin")}</h1>
              <p className="mt-2 text-sm text-muted-foreground">
                {t("This area is for system administrators.")}
              </p>
            </div>
          ) : (
            <Outlet />
          )}
        </main>
      </div>
    </div>
  );
}
