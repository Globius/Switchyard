// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { X } from "lucide-react";
import { Dialog } from "radix-ui";
import type { ReactNode } from "react";
import { t } from "@/lib/i18n";

/** Right-hand edit sheet (pools, monitors, providers are edited in sheets, per the design). */
export function Sheet({
  open,
  onOpenChange,
  title,
  badge,
  children,
  footer,
  centered,
  inline,
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: string;
  badge?: ReactNode;
  children: ReactNode;
  footer: ReactNode;
  /** Centred dialog instead of a right-hand sheet (short forms such as issuing a certificate). */
  centered?: boolean;
  /** Render as a pane in the page (master-detail editors) instead of an overlay. */
  inline?: boolean;
}) {
  if (inline)
    return (
      <section aria-label={title} className="flex min-h-[28rem] flex-col rounded-lg border bg-card">
        <div className="flex items-center gap-3 border-b px-5 py-3">
          <h2 className="text-base font-semibold">{title}</h2>
          {badge}
          <button
            type="button"
            className="ml-auto rounded-md p-1 hover:bg-accent"
            aria-label={t("Close")}
            onClick={() => onOpenChange(false)}
          >
            <X className="size-4" />
          </button>
        </div>
        <div className="flex-1 space-y-5 p-5">{children}</div>
        <div className="flex items-center gap-2 border-t px-5 py-3">{footer}</div>
      </section>
    );
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30" />
        <Dialog.Content
          className={
            centered
              ? "fixed top-1/2 left-1/2 z-50 flex max-h-[90vh] w-[min(34rem,94vw)] -translate-x-1/2 -translate-y-1/2 flex-col rounded-xl border bg-card shadow-2xl"
              : "fixed inset-y-0 right-0 z-50 flex w-[min(40rem,100vw)] flex-col border-l bg-card shadow-2xl"
          }
        >
          <div className="flex items-center gap-3 border-b px-5 py-3">
            <Dialog.Title className="text-base font-semibold">{title}</Dialog.Title>
            {badge}
            <Dialog.Close
              className="ml-auto rounded-md p-1 hover:bg-accent"
              aria-label={t("Close")}
            >
              <X className="size-4" />
            </Dialog.Close>
          </div>
          <Dialog.Description className="sr-only">{title}</Dialog.Description>
          <div className="flex-1 space-y-5 overflow-y-auto p-5">{children}</div>
          <div className="flex items-center gap-2 border-t px-5 py-3">{footer}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
