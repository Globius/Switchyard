// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Whether the server has said the session ended (a 401 `session_expired`), for the re-login modal. */
import { useSyncExternalStore } from "react";

let expired = false;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

export function markSessionExpired(): void {
  if (expired) return;
  expired = true;
  emit();
}

export function clearSessionExpired(): void {
  if (!expired) return;
  expired = false;
  emit();
}

export function useSessionExpired(): boolean {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => expired,
    () => false,
  );
}
