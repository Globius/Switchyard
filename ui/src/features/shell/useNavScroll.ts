// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { useRouterState } from "@tanstack/react-router";
import { useEffect, type RefObject } from "react";

/**
 * Keep the active link of a scrollable nav in view.
 *
 * Runs on first render and after every navigation: finds the link marked
 * `aria-current="page"` (TanStack Router sets it on the active `Link`) and
 * scrolls the nav just enough to show it (`block: "nearest"`, so an item that
 * is already visible does not move). Only the nav scrolls, never the page.
 *
 * Example:
 *     const ref = useRef<HTMLElement>(null);
 *     useKeepActiveInView(ref);
 *     return <nav ref={ref} className="overflow-y-auto">…</nav>;
 */
export function useKeepActiveInView(ref: RefObject<HTMLElement | null>): void {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  useEffect(() => {
    const el = ref.current?.querySelector<HTMLElement>('a[aria-current="page"]');
    // jsdom has no scrollIntoView; real browsers do.
    el?.scrollIntoView?.({ block: "nearest" });
  }, [ref, pathname]);
}
