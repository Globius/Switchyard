import { Link, Outlet } from "@tanstack/react-router";
import { Activity, Moon, Search, Signpost, Sun, WifiOff } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";
import { ChangeSetBar } from "./ChangeSetBar";
import { CommandPalette } from "./CommandPalette";
import { useDraft } from "./draft";
import { useStreamState } from "./stream";
import { TenantSwitcher } from "./TenantSwitcher";
import { UserMenu } from "./UserMenu";

/** Nav sections from the design handoff. `to` is the path segment under /t/:tenant. */
export const NAV: { heading?: string; items: { label: string; to: string }[] }[] = [
  { items: [{ label: "Home", to: "" }] },
  {
    heading: "Traffic",
    items: [
      { label: "Virtual services", to: "virtual-services" },
      { label: "Pools", to: "pools" },
      { label: "Health monitors", to: "health-monitors" },
      { label: "Certificates", to: "certificates" },
      { label: "Policies", to: "policies" },
    ],
  },
  {
    heading: "Infrastructure",
    items: [
      { label: "Networks and VIP pools", to: "networks" },
      { label: "SE groups", to: "se-groups" },
      { label: "Routing profiles", to: "routing-profiles" },
    ],
  },
  {
    heading: "Changes",
    items: [
      { label: "Change sets", to: "change-sets" },
      { label: "Revisions", to: "revisions" },
      { label: "Jobs", to: "jobs" },
    ],
  },
  { heading: "Tenant", items: [{ label: "Access and settings", to: "access" }] },
];

function StreamIndicator({ tenant }: { tenant: string }) {
  const s = useStreamState(tenant);
  if (s === "live")
    return (
      <Badge tone="healthy">
        <Activity className="size-3" /> {t("Stream live")}
      </Badge>
    );
  if (s === "down")
    return (
      <Badge tone="down">
        <WifiOff className="size-3" /> {t("Stream down")}
      </Badge>
    );
  return <Badge>{t("Connecting")}</Badge>;
}

function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const dark = theme === "dark";
  return (
    <Button
      variant="ghost"
      size="icon"
      aria-label={dark ? t("Switch to light theme") : t("Switch to dark theme")}
      onClick={() => setTheme(dark ? "light" : "dark")}
    >
      {dark ? <Sun /> : <Moon />}
    </Button>
  );
}

/** Top bar, left nav and the change-set bar around every tenant page. */
export function Shell({ tenant, isSystem }: { tenant: string; isSystem: boolean }) {
  const [palette, setPalette] = useState(false);
  const { draft } = useDraft(tenant);
  const sections = isSystem
    ? [...NAV, { heading: "System", items: [{ label: "Admin", to: "../../admin/tenants" }] }]
    : NAV;
  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex h-14 items-center gap-3 border-b bg-card px-4">
        <Link to="/t/$tenant" params={{ tenant }} className="flex items-center gap-2 font-semibold">
          <span className="grid size-7 place-items-center rounded-md bg-primary text-primary-foreground">
            <Signpost className="size-4" aria-hidden />
          </span>
          Switchyard
        </Link>
        <TenantSwitcher tenant={tenant} />
        <button
          type="button"
          onClick={() => setPalette(true)}
          className="ml-auto flex w-80 items-center gap-2 rounded-md border bg-background px-3 py-1.5 text-sm text-muted-foreground"
        >
          <Search className="size-4" />
          <span className="flex-1 text-left">{t("Jump to any object")}</span>
          <kbd className="rounded border px-1.5 font-mono text-xs">Ctrl K</kbd>
        </button>
        <StreamIndicator tenant={tenant} />
        <ThemeToggle />
        <UserMenu />
      </header>
      <div className="flex flex-1">
        <nav aria-label={t("Sections")} className="w-56 shrink-0 border-r bg-sidebar p-3 text-sm">
          {sections.map((s) => (
            <div key={s.heading ?? "home"} className="mb-2">
              {s.heading && (
                <div className="px-3 py-2 text-xs font-medium tracking-wide text-muted-foreground uppercase">
                  {t(s.heading)}
                </div>
              )}
              {s.items.map((i) =>
                i.to.startsWith("..") ? (
                  <Link
                    key={i.label}
                    to="/admin/tenants"
                    className="block rounded-md px-3 py-2 hover:bg-sidebar-accent"
                  >
                    {t(i.label)}
                  </Link>
                ) : (
                  <Link
                    key={i.label}
                    to="/t/$tenant/$"
                    params={{ tenant, _splat: i.to }}
                    activeOptions={{ exact: true }}
                    className="block rounded-md px-3 py-2 hover:bg-sidebar-accent"
                    activeProps={{ className: "bg-sidebar-accent font-medium" }}
                  >
                    {t(i.label)}
                  </Link>
                ),
              )}
            </div>
          ))}
        </nav>
        <main className={draft ? "min-w-0 flex-1 p-8 pb-24" : "min-w-0 flex-1 p-8"}>
          <Outlet />
        </main>
      </div>
      {draft && <ChangeSetBar tenant={tenant} draft={draft} />}
      <CommandPalette tenant={tenant} draft={draft} open={palette} onOpenChange={setPalette} />
    </div>
  );
}
