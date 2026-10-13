// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from "vitest";
import {
  HAPROXY_KEYWORDS,
  HAPROXY_SECTIONS,
  classifyHaproxyLine,
  diffLanguage,
  haproxyLanguage,
} from "@/lib/haproxy-language";
import { loadMonaco, monacoUnavailable } from "@/lib/monaco";

describe("config highlighting", () => {
  it("classifies comments, sections and directives", () => {
    expect(classifyHaproxyLine("# a comment")).toBe("comment");
    expect(classifyHaproxyLine("frontend vs_web")).toBe("section");
    expect(classifyHaproxyLine("    bind 10.0.0.1:443 ssl")).toBe("directive");
    expect(classifyHaproxyLine("    monitor-uri /healthz")).toBe("other");
  });

  it("exposes the section and keyword lists to the tokenizer", () => {
    expect(haproxyLanguage.sections).toBe(HAPROXY_SECTIONS);
    expect(haproxyLanguage.keywords).toBe(HAPROXY_KEYWORDS);
    expect(diffLanguage.tokenizer.root.length).toBeGreaterThan(2);
  });

  it("does not load Monaco in tests; callers show the plain view", async () => {
    expect(monacoUnavailable()).toBe(true);
    expect(await loadMonaco()).toBeNull();
  });
});
