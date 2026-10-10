import "@testing-library/jest-dom/vitest";

// jsdom lacks these; cmdk and Radix use them.
globalThis.ResizeObserver ??= class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
Element.prototype.scrollIntoView ??= () => {};

// Streams are not under test here; fake an EventSource that connects.
class FakeEventSource {
  static CLOSED = 2;
  readyState = 1;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor() {
    queueMicrotask(() => this.onopen?.());
  }
  close() {}
}
globalThis.EventSource = FakeEventSource as unknown as typeof EventSource;
