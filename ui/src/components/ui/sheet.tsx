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
}: {
  open: boolean;
  onOpenChange: (o: boolean) => void;
  title: string;
  badge?: ReactNode;
  children: ReactNode;
  footer: ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30" />
        <Dialog.Content className="fixed inset-y-0 right-0 z-50 flex w-[min(40rem,100vw)] flex-col border-l bg-card shadow-2xl">
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
