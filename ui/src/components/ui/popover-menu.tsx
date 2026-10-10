import { DropdownMenu as M } from "radix-ui";
import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

export const Menu = M.Root;
export const MenuTrigger = M.Trigger;

export function MenuContent({ className, ...p }: ComponentProps<typeof M.Content>) {
  return (
    <M.Portal>
      <M.Content
        align="end"
        sideOffset={6}
        className={cn(
          "z-50 min-w-64 rounded-lg border bg-popover p-1 text-popover-foreground shadow-lg",
          className,
        )}
        {...p}
      />
    </M.Portal>
  );
}
