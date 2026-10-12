import { Link, Outlet } from "@tanstack/react-router";
import { Activity, Menu, Moon, Search, Signpost, Sun, WifiOff } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { useTheme } from "@/lib/theme";
import { ChangeSetBar } from "./ChangeSetBar";
import { CommandPalette } from "./CommandPalette";
import { useDraft } from "./draft";
import { useTenantStream, type Stream, type StreamState } from "./stream";
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
  {
    heading: "Tenant",
    items: [
      { label: "Access and settings", to: "access" },
      { label: "DNS credentials", to: "dns-credentials" },
    ],
  },
];

function StreamIndicator({ s }: { s: StreamState }) {
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

/** Shown while live updates are paused: values on the page may be stale until the stream is back. */
function StreamBanner({ stream }: { stream: Stream }) {
  if (stream.state !== "down") return null;
  return (
    <div
      role="alert"
      className="flex items-center gap-3 border-b bg-status-degraded/15 px-4 py-2 text-sm"
    >
      <WifiOff className="size-4" aria-hidden />
      <span>
        {t("Live updates paused. Values may be out of date.")}{" "}
        {stream.retryIn != null && t("Reconnecting in {n} s", { n: stream.retryIn })}
      </span>
      <Button size="sm" variant="outline" className="ml-auto" onClick={stream.retryNow}>
        {t("Retry now")}
      </Button>
    </div>
  );
}

/** Top bar, left nav and the change-set bar around every tenant page. */
export function Shell({ tenant, isSystem }: { tenant: string; isSystem: boolean }) {
  const [palette, setPalette] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const stream = useTenantStream(tenant);
  const { draft } = useDraft(tenant);
  const sections = isSystem
    ? [...NAV, { heading: "System", items: [{ label: "Admin", to: "../../admin/tenants" }] }]
    : NAV;
  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex h-14 items-center gap-2 border-b bg-card px-3 sm:gap-3 sm:px-4">
        <Button
          variant="ghost"
          size="icon"
          className="md:hidden"
          aria-label={navOpen ? t("Close menu") : t("Open menu")}
          aria-expanded={navOpen}
          onClick={() => setNavOpen((o) => !o)}
        >
          <Menu />
        </Button>
        <Link to="/t/$tenant" params={{ tenant }} className="flex items-center gap-2 font-semibold">
          <span className="grid size-7 place-items-center rounded-md bg-primary text-primary-foreground">
            <Signpost className="size-4" aria-hidden />
          </span>
          <span className="hidden sm:inline">Switchyard</span>
        </Link>
        <span aria-hidden className="hidden h-6 w-px bg-border sm:block" />
        <TenantSwitcher tenant={tenant} />
        <button
          type="button"
          onClick={() => setPalette(true)}
          aria-label={t("Jump to any object")}
          className="ml-auto flex items-center gap-2 rounded-md border bg-background px-2.5 py-1.5 text-sm text-muted-foreground lg:w-80 lg:px-3"
        >
          <Search className="size-4" />
          <span className="hidden flex-1 text-left lg:inline">{t("Jump to any object")}</span>
          <kbd className="hidden rounded border px-1.5 font-mono text-xs lg:inline">Ctrl K</kbd>
        </button>
        <span className="hidden sm:inline-flex">
          <StreamIndicator s={stream.state} />
        </span>
        <ThemeToggle />
        <UserMenu />
      </header>
      <StreamBanner stream={stream} />
      <div className="flex flex-1">
        {navOpen && (
          <button
            type="button"
            aria-label={t("Close menu")}
            className="fixed inset-0 top-14 z-30 bg-black/30 md:hidden"
            onClick={() => setNavOpen(false)}
          />
        )}
        <nav
          aria-label={t("Sections")}
          onClick={(e) => (e.target as HTMLElement).closest("a") && setNavOpen(false)}
          className={cn(
            "w-56 shrink-0 border-r bg-sidebar p-3 text-sm",
            navOpen
              ? "fixed inset-y-0 top-14 left-0 z-40 overflow-y-auto shadow-xl md:static md:shadow-none"
              : "hidden md:block",
          )}
        >
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
        <main className={cn("min-w-0 flex-1 p-4 md:p-8", draft && "pb-24")}>
          <Outlet key={tenant} />
        </main>
      </div>
      {draft && <ChangeSetBar tenant={tenant} draft={draft} />}
      <CommandPalette tenant={tenant} draft={draft} open={palette} onOpenChange={setPalette} />
    </div>
  );
}
