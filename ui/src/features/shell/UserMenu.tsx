import { Link } from "@tanstack/react-router";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Menu, MenuContent, MenuTrigger } from "@/components/ui/popover-menu";
import { useLogout, useMe, useRefreshAccess } from "@/auth/session";
import { t } from "@/lib/i18n";
import { useTheme, type Theme } from "@/lib/theme";

const THEMES: Theme[] = ["light", "dark", "system"];

/** Up to two initials of a display name, for the avatar. */
export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");
}

/** Who you are, which roles you hold and why, access refresh, session end, theme, sign out. */
export function UserMenu() {
  const { data: me } = useMe();
  const refresh = useRefreshAccess();
  const logout = useLogout();
  const { theme, setTheme } = useTheme();
  if (!me) return null;
  return (
    <Menu>
      <MenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={t("User menu")} className="rounded-full">
          <span className="grid size-8 place-items-center rounded-full bg-secondary text-xs font-semibold">
            {initials(me.name)}
          </span>
        </Button>
      </MenuTrigger>
      <MenuContent className="w-80 p-0">
        <div className="border-b p-4">
          <div className="font-semibold">{me.name}</div>
          <div className="text-xs text-muted-foreground">{me.email}</div>
        </div>
        <div className="space-y-2 border-b p-4">
          <div className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            {t("Your roles")}
          </div>
          {me.is_system && (
            <div className="flex items-center justify-between text-sm">
              <span>{t("All tenants")}</span>
              <Badge tone="pending">system-admin</Badge>
            </div>
          )}
          {me.tenants.map((x) => (
            <div key={x.id} className="flex items-start justify-between gap-2 text-sm">
              <div>
                <div>{x.name}</div>
                {x.via.map((v) => (
                  <div
                    key={v.provider + v.group}
                    className="font-mono text-xs text-muted-foreground"
                  >
                    {t("via {group}", { group: v.group })}
                  </div>
                ))}
              </div>
              <Badge>{x.role}</Badge>
            </div>
          ))}
          <Button
            variant="link"
            className="h-auto p-0"
            disabled={refresh.isPending}
            onClick={() => refresh.mutate()}
          >
            {t("Refresh access")}
          </Button>
          <p className="text-xs text-muted-foreground">
            {t(
              "Mapping changes reach you within a minute. Group changes at your provider need a new sign-in.",
            )}
          </p>
        </div>
        {me.session && (
          <div className="border-b px-4 py-2 text-xs text-muted-foreground">
            {t("Session ends at {at} · {min} min idle limit", {
              at: new Date(me.session.expires_at).toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit",
              }),
              min: Math.round(me.session.idle_timeout_seconds / 60),
            })}
          </div>
        )}
        <div className="flex items-center justify-between border-b px-4 py-3 text-sm">
          {t("Theme")}
          <div
            role="group"
            aria-label={t("Theme")}
            className="flex overflow-hidden rounded-md border"
          >
            {THEMES.map((x) => (
              <button
                key={x}
                type="button"
                aria-pressed={theme === x}
                onClick={() => setTheme(x)}
                className="px-3 py-1 text-sm capitalize aria-pressed:bg-accent"
              >
                {t(x)}
              </button>
            ))}
          </div>
        </div>
        <div className="flex flex-col p-1 text-sm">
          {me.is_system && (
            <Link to="/admin/tenants" className="rounded-md px-3 py-2 hover:bg-accent">
              {t("Admin")}
            </Link>
          )}
          <button
            type="button"
            className="rounded-md px-3 py-2 text-left hover:bg-accent"
            onClick={() => logout.mutate()}
          >
            {t("Sign out")}
          </button>
        </div>
      </MenuContent>
    </Menu>
  );
}
