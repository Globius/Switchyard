// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { Tooltip as T } from "radix-ui";
import type { ReactNode } from "react";

/** Wraps `children` with a tooltip; renders `children` untouched when `label` is empty. */
export function Tip({ label, children }: { label?: string | null; children: ReactNode }) {
  if (!label) return <>{children}</>;
  return (
    <T.Provider delayDuration={200}>
      <T.Root>
        <T.Trigger asChild>
          <span className="inline-flex">{children}</span>
        </T.Trigger>
        <T.Portal>
          <T.Content
            sideOffset={6}
            className="z-50 max-w-xs rounded-md bg-foreground px-3 py-1.5 text-xs text-background"
          >
            {label}
          </T.Content>
        </T.Portal>
      </T.Root>
    </T.Provider>
  );
}
