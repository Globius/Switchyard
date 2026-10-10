/** One SSE connection per tab for the tenant; exposes its connection state to the top bar. */
import { useEffect, useState } from "react";

export type StreamState = "connecting" | "live" | "down";

export function useStreamState(tenant: string): StreamState {
  const [state, setState] = useState<StreamState>("connecting");
  useEffect(() => {
    if (typeof EventSource === "undefined") return;
    const es = new EventSource(`/api/v1/tenants/${encodeURIComponent(tenant)}/status/stream`);
    es.onopen = () => setState("live");
    es.onerror = () => setState(es.readyState === EventSource.CLOSED ? "down" : "connecting");
    return () => es.close();
  }, [tenant]);
  return state;
}
