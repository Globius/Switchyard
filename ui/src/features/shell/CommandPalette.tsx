import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { Command } from "cmdk";
import { Pencil, Plus, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { api, unwrap } from "@/api/client";
import { t } from "@/lib/i18n";
import type { ChangeSetSummary } from "./draft";

const GROUP: Record<string, string> = {
  virtual_service: "Virtual services",
  pool: "Pools",
  health_monitor: "Health monitors",
  certificate: "Certificates",
  policy_set: "Policies",
  network: "Networks",
  vip_pool: "VIP pools",
  se_group: "SE groups",
  routing_profile: "Routing profiles",
};

const SECTION: Record<string, string> = {
  pool: "pools",
  health_monitor: "health-monitors",
  certificate: "certificates",
  policy_set: "policies",
  network: "networks",
  vip_pool: "networks",
  se_group: "se-groups",
  routing_profile: "routing-profiles",
};

const HEADING =
  "[&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:py-1.5 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-medium [&_[cmdk-group-heading]]:tracking-wide [&_[cmdk-group-heading]]:text-muted-foreground [&_[cmdk-group-heading]]:uppercase";
const ITEM =
  "flex cursor-pointer items-center gap-2 rounded-md px-3 py-2 text-sm aria-selected:bg-accent";

/** Ctrl/Cmd+K jump to any object by name (`GET /tenants/{t}/search`) plus two actions. */
export function CommandPalette({
  tenant,
  draft,
  open,
  onOpenChange,
}: {
  tenant: string;
  draft?: ChangeSetSummary;
  open: boolean;
  onOpenChange: (o: boolean) => void;
}) {
  const [q, setQ] = useState("");
  const navigate = useNavigate();
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        onOpenChange(!open);
      }
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onOpenChange]);
  const hits = useQuery({
    queryKey: ["t", tenant, "search", q],
    enabled: open && q.trim().length > 0,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/search", {
          params: { path: { t: tenant }, query: { q, limit: 20 } },
        }),
      ),
  });
  const groups = new Map<string, NonNullable<typeof hits.data>["hits"]>();
  for (const h of hits.data?.hits ?? []) groups.set(h.type, [...(groups.get(h.type) ?? []), h]);
  const go = (fn: () => void) => {
    onOpenChange(false);
    setQ("");
    fn();
  };
  const open_ = (type: string, id: string) =>
    type === "virtual_service"
      ? navigate({ to: "/t/$tenant/virtual-services/$id", params: { tenant, id } })
      : navigate({ to: "/t/$tenant/$", params: { tenant, _splat: SECTION[type] ?? "" } });
  return (
    <Command.Dialog
      open={open}
      onOpenChange={onOpenChange}
      shouldFilter={false}
      label={t("Jump to any object")}
      overlayClassName="fixed inset-0 z-50 bg-black/30"
      contentClassName="fixed top-24 left-1/2 z-50 w-[min(640px,92vw)] -translate-x-1/2 overflow-hidden rounded-xl border bg-popover shadow-2xl"
    >
      <div className="flex items-center gap-3 border-b px-4">
        <Search className="size-4 text-muted-foreground" />
        <Command.Input
          value={q}
          onValueChange={setQ}
          placeholder={t("Jump to any object")}
          className="h-12 flex-1 bg-transparent text-sm outline-none"
        />
        <kbd className="rounded border px-1.5 font-mono text-xs text-muted-foreground">Esc</kbd>
      </div>
      <Command.List className="max-h-96 overflow-auto p-2">
        {q.trim() && !hits.isFetching && (hits.data?.hits.length ?? 0) === 0 && (
          <Command.Empty className="px-3 py-6 text-center text-sm text-muted-foreground">
            {t("Nothing matches “{q}”.", { q })}
          </Command.Empty>
        )}
        {[...groups].map(([type, items]) => (
          <Command.Group key={type} heading={t(GROUP[type] ?? type)} className={HEADING}>
            {items.map((h) => (
              <Command.Item
                key={h.id}
                value={h.id}
                onSelect={() => go(() => void open_(h.type, h.id))}
                className={`${ITEM} justify-between`}
              >
                <span className="font-medium text-primary">{h.name}</span>
                <span className="font-mono text-xs text-muted-foreground">{h.detail}</span>
              </Command.Item>
            ))}
          </Command.Group>
        ))}
        <Command.Group heading={t("Actions")} className={HEADING}>
          <Command.Item
            value="action-new-vs"
            onSelect={() => go(() => void navigate({ to: "/t/$tenant/new", params: { tenant } }))}
            className={ITEM}
          >
            <Plus className="size-4" /> {t("New virtual service")}
          </Command.Item>
          {draft && (
            <Command.Item
              value="action-review"
              onSelect={() =>
                go(
                  () =>
                    void navigate({
                      to: "/t/$tenant/review/$cs",
                      params: { tenant, cs: draft.id },
                    }),
                )
              }
              className={ITEM}
            >
              <Pencil className="size-4" /> {t("Review my draft")}
              <span className="ml-auto text-xs text-muted-foreground">
                {t("{n} changes", { n: draft.op_count })}
              </span>
            </Command.Item>
          )}
        </Command.Group>
      </Command.List>
      <div className="border-t px-4 py-2 text-xs text-muted-foreground">
        {t("Searching {tenant}", { tenant })}
      </div>
    </Command.Dialog>
  );
}
