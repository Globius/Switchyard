// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { AlertDialog } from "radix-ui";
import type { ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";

/** Runtime actions skip the change set, so they always confirm first (with a Live note). */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  children,
  confirmLabel,
  onConfirm,
  busy,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  busy?: boolean;
}) {
  return (
    <AlertDialog.Root open={open} onOpenChange={onOpenChange}>
      <AlertDialog.Portal>
        <AlertDialog.Overlay className="fixed inset-0 z-50 bg-black/40" />
        <AlertDialog.Content className="fixed top-1/3 left-1/2 z-50 w-[min(420px,92vw)] -translate-x-1/2 space-y-3 rounded-xl border bg-card p-5 shadow-2xl">
          <AlertDialog.Title className="text-base font-semibold">{title}</AlertDialog.Title>
          <AlertDialog.Description asChild>
            <div className="space-y-2 text-sm text-muted-foreground">{children}</div>
          </AlertDialog.Description>
          <div className="flex justify-end gap-2 pt-2">
            <AlertDialog.Cancel asChild>
              <Button variant="outline">{t("Cancel")}</Button>
            </AlertDialog.Cancel>
            <Button disabled={busy} onClick={onConfirm}>
              {confirmLabel}
            </Button>
          </div>
        </AlertDialog.Content>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
