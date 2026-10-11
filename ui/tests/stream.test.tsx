import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { applyEvent, backoff, useTenantStream } from "@/features/shell/stream";

class FakeES {
  static instances: FakeES[] = [];
  static readonly CLOSED = 2;
  static readonly OPEN = 1;
  readyState = 0;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  listeners = new Map<string, (e: MessageEvent<string>) => void>();
  constructor(public url: string) {
    FakeES.instances.push(this);
  }
  addEventListener(t: string, f: (e: MessageEvent<string>) => void) {
    this.listeners.set(t, f);
  }
  close() {
    this.readyState = FakeES.CLOSED;
  }
  open() {
    this.readyState = FakeES.OPEN;
    this.onopen?.();
  }
  emit(type: string, data: unknown) {
    this.listeners.get(type)?.({ data: JSON.stringify(data) } as MessageEvent<string>);
  }
  fail(closed: boolean) {
    this.readyState = closed ? FakeES.CLOSED : 0;
    this.onerror?.();
  }
}

describe("stream cache patching", () => {
  it("backs off 1, 2, 4, 8, then 15 seconds", () => {
    expect([0, 1, 2, 3, 4, 9].map(backoff)).toEqual([1, 2, 4, 8, 15, 15]);
  });

  it("writes status and vs_state into the cache and invalidates what depends on jobs and revisions", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, "acme", "status", [{ engine_id: "e1" }]);
    expect(qc.getQueryData(["t", "acme", "status", "engines"])).toEqual([{ engine_id: "e1" }]);
    applyEvent(qc, "acme", "vs_state", [{ id: "v1" }]);
    expect(qc.getQueryData(["t", "acme", "status", "virtual-services"])).toEqual([{ id: "v1" }]);
    applyEvent(qc, "acme", "job_progress", { id: "j" });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["t", "acme", "jobs"] });
    spy.mockClear();
    applyEvent(qc, "acme", "revision_applied", { number: 42 });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["t", "acme"] });
    applyEvent(qc, "acme", "nonsense", {});
  });
});

describe("useTenantStream", () => {
  beforeEach(() => {
    FakeES.instances = [];
    vi.useFakeTimers();
    vi.stubGlobal("EventSource", FakeES);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const wrap = (qc: QueryClient) =>
    function W({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
    };

  it("goes live, patches on events, shows down with a countdown, reconnects and refetches", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    const { result } = renderHook(() => useTenantStream("acme"), { wrapper: wrap(qc) });
    expect(result.current.state).toBe("connecting");
    const first = FakeES.instances[0]!;
    expect(first.url).toBe("/api/v1/tenants/acme/status/stream");
    act(() => first.open());
    expect(result.current.state).toBe("live");
    act(() => first.emit("status", [{ engine_id: "e9" }]));
    expect(qc.getQueryData(["t", "acme", "status", "engines"])).toEqual([{ engine_id: "e9" }]);
    // A malformed event is ignored.
    act(() => first.listeners.get("status")?.({ data: "{oops" } as MessageEvent<string>));

    act(() => first.fail(false));
    expect(result.current.state).toBe("connecting");
    act(() => first.fail(true));
    expect(result.current.state).toBe("down");
    expect(result.current.retryIn).toBe(1);
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(FakeES.instances).toHaveLength(2);
    spy.mockClear();
    act(() => FakeES.instances[1]!.open());
    expect(result.current.state).toBe("live");
    expect(result.current.retryIn).toBeNull();
    expect(spy).toHaveBeenCalledWith({ queryKey: ["t", "acme"] });
  });

  it("retry now reconnects without waiting", () => {
    const qc = new QueryClient();
    const { result } = renderHook(() => useTenantStream("acme"), { wrapper: wrap(qc) });
    act(() => FakeES.instances[0]!.fail(true));
    expect(result.current.state).toBe("down");
    act(() => result.current.retryNow());
    expect(FakeES.instances).toHaveLength(2);
  });
});
