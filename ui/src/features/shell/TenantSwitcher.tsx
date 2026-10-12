// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useNavigate } from "@tanstack/react-router";
import { Check, ChevronDown } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Menu, MenuContent, MenuTrigger } from "@/components/ui/popover-menu";
import { Badge } from "@/components/ui/badge";
import { useTenants } from "@/auth/session";
import { t } from "@/lib/i18n";

/** Tenant name is always visible in the top bar; switching swaps the URL and every query. */
export function TenantSwitcher({ tenant }: { tenant: string }) {
  const { data } = useTenants();
  const [q, setQ] = useState("");
  const navigate = useNavigate();
  const shown = (data ?? []).filter((x) => x.name.toLowerCase().includes(q.toLowerCase()));
  return (
    <Menu onOpenChange={() => setQ("")}>
      <MenuTrigger asChild>
        <Button variant="outline" aria-label={t("Switch tenant")}>
          <span className="text-muted-foreground">{t("Tenant")}</span>
          <span className="font-semibold">{tenant}</span>
          <ChevronDown />
        </Button>
      </MenuTrigger>
      <MenuContent align="start" className="w-80">
        <input
          aria-label={t("Find tenant")}
          placeholder={t("Find tenant")}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.stopPropagation()}
          className="mb-1 w-full rounded-md border bg-card px-3 py-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        {shown.map((x) => (
          <button
            key={x.id}
            type="button"
            onClick={() => void navigate({ to: "/t/$tenant", params: { tenant: x.name } })}
            className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm hover:bg-accent"
          >
            <span className="w-4">{x.name === tenant && <Check className="size-4" />}</span>
            <span className="flex-1 text-primary">{x.name}</span>
            {x.require_approval && <Badge tone="pending">{t("approval on")}</Badge>}
          </button>
        ))}
        {data && shown.length === 0 && (
          <div className="px-3 py-2 text-sm text-muted-foreground">{t("No tenant matches.")}</div>
        )}
        <p className="border-t px-3 pt-2 pb-1 text-xs text-muted-foreground">
          {t("Switching tenant changes the URL and every page's context. Your draft stays open.")}
        </p>
      </MenuContent>
    </Menu>
  );
}
