/**
 * One SSE connection per tab for the tenant.
 *
 * Events patch the query cache (`status`, `vs_state`, `job_progress`, `revision_applied`) so live
 * pages update without polling. When the connection drops the hook reports `down`, retries with
 * backoff and, once it is back, refetches the tenant's queries (a reconnect restarts from a
 * snapshot, so nothing is replayed).
 */
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

export type StreamState = "connecting" | "live" | "down";

export interface Stream {
  state: StreamState;
  /** Seconds until the next reconnect attempt while `down`. */
  retryIn: number | null;
  /** Reconnect now instead of waiting. */
  retryNow: () => void;
}

/** Backoff between reconnect attempts, in seconds: 1, 2, 4, 8, then 15. */
export function backoff(attempt: number): number {
  return Math.min(15, 2 ** Math.max(0, attempt));
}

/** Patch the cache from one event. Unknown events are ignored. */
export function applyEvent(qc: QueryClient, tenant: string, type: string, data: unknown): void {
  const base = ["t", tenant] as const;
  switch (type) {
    case "status":
      qc.setQueryData([...base, "status", "engines"], data);
      void qc.invalidateQueries({ queryKey: [...base, "summary"] });
      break;
    case "vs_state":
      qc.setQueryData([...base, "status", "virtual-services"], data);
      void qc.invalidateQueries({ queryKey: [...base, "summary"] });
      break;
    case "job_progress":
      void qc.invalidateQueries({ queryKey: [...base, "jobs"] });
      void qc.invalidateQueries({ queryKey: [...base, "change-sets"] });
      void qc.invalidateQueries({ queryKey: [...base, "summary"] });
      break;
    case "revision_applied":
      // Desired state changed: every list, page and diff may differ now.
      void qc.invalidateQueries({ queryKey: base });
      break;
  }
}

const EVENTS = ["status", "vs_state", "job_progress", "revision_applied"] as const;

export function useTenantStream(tenant: string): Stream {
  const qc = useQueryClient();
  const [state, setState] = useState<StreamState>("connecting");
  const [retryIn, setRetryIn] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);
  const attempt = useRef(0);
  const wasDown = useRef(false);

  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    let timer: ReturnType<typeof setInterval> | undefined;
    const es = new EventSource(`/api/v1/tenants/${encodeURIComponent(tenant)}/status/stream`);
    es.onopen = () => {
      attempt.current = 0;
      setState("live");
      setRetryIn(null);
      if (wasDown.current) {
        wasDown.current = false;
        void qc.invalidateQueries({ queryKey: ["t", tenant] });
      }
    };
    for (const type of EVENTS)
      es.addEventListener(type, (e) => {
        try {
          applyEvent(qc, tenant, type, JSON.parse((e as MessageEvent<string>).data));
        } catch {
          // A malformed event is dropped; the next snapshot corrects it.
        }
      });
    es.onerror = () => {
      wasDown.current = true;
      if (es.readyState !== EventSource.CLOSED) {
        setState("connecting");
        return;
      }
      setState("down");
      let left = backoff(attempt.current++);
      setRetryIn(left);
      timer = setInterval(() => {
        left -= 1;
        if (left <= 0) {
          clearInterval(timer);
          setNonce((n) => n + 1);
        } else setRetryIn(left);
      }, 1000);
    };
    return () => {
      clearInterval(timer);
      es.close();
    };
  }, [tenant, qc, nonce]);

  const retryNow = useCallback(() => setNonce((n) => n + 1), []);
  return { state, retryIn, retryNow };
}

/** Connection state only; kept for callers that do not need the banner. */
export function useStreamState(tenant: string): StreamState {
  return useTenantStream(tenant).state;
}
