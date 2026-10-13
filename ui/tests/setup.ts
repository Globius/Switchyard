// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";

// Full-route renders are slow when the whole suite runs in parallel.
configure({ asyncUtilTimeout: 5000 });

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
  addEventListener() {}
  close() {}
}
globalThis.EventSource = FakeEventSource as unknown as typeof EventSource;
