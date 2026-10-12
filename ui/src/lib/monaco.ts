// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Lazy Monaco loader. The editor is only needed on the config screens, so it is imported on first
 * use and kept out of the main bundle. Tests and browsers that cannot run it get `null` and the
 * callers fall back to a plain view.
 */
import type * as MonacoNs from "monaco-editor/esm/vs/editor/editor.api";
import { diffLanguage, haproxyLanguage } from "./haproxy-language";

export type Monaco = typeof MonacoNs;

let loading: Promise<Monaco | null> | null = null;

/** True when Monaco cannot run here (unit tests, no workers), so callers render the plain view. */
export function monacoUnavailable(): boolean {
  return import.meta.env.MODE === "test" || typeof Worker === "undefined";
}

export function loadMonaco(): Promise<Monaco | null> {
  if (monacoUnavailable()) return Promise.resolve(null);
  loading ??= (async () => {
    try {
      const [monaco, worker] = await Promise.all([
        import("monaco-editor/esm/vs/editor/editor.api"),
        import("monaco-editor/esm/vs/editor/editor.worker?worker"),
      ]);
      (self as unknown as { MonacoEnvironment: unknown }).MonacoEnvironment = {
        getWorker: () => new worker.default(),
      };
      monaco.languages.register({ id: "haproxy" });
      monaco.languages.setMonarchTokensProvider(
        "haproxy",
        haproxyLanguage as unknown as MonacoNs.languages.IMonarchLanguage,
      );
      monaco.languages.register({ id: "sy-diff" });
      monaco.languages.setMonarchTokensProvider(
        "sy-diff",
        diffLanguage as unknown as MonacoNs.languages.IMonarchLanguage,
      );
      const rules = [
        { token: "comment", foreground: "6b7280", fontStyle: "italic" },
        { token: "type.identifier", foreground: "7c3aed", fontStyle: "bold" },
        { token: "keyword", foreground: "1d4ed8" },
        { token: "string", foreground: "047857" },
        { token: "number", foreground: "b45309" },
        { token: "number.float", foreground: "b45309" },
        { token: "diff.add", foreground: "047857" },
        { token: "diff.del", foreground: "b91c1c" },
        { token: "meta", foreground: "6b7280" },
      ];
      monaco.editor.defineTheme("sy-light", {
        base: "vs",
        inherit: true,
        rules,
        colors: { "editor.background": "#00000000" },
      });
      monaco.editor.defineTheme("sy-dark", {
        base: "vs-dark",
        inherit: true,
        rules: rules.map((r) => ({
          ...r,
          foreground:
            {
              "1d4ed8": "93c5fd",
              "047857": "6ee7b7",
              b45309: "fcd34d",
              "7c3aed": "c4b5fd",
              b91c1c: "fca5a5",
              "6b7280": "9ca3af",
            }[r.foreground] ?? r.foreground,
        })),
        colors: { "editor.background": "#00000000" },
      });
      return monaco;
    } catch {
      return null;
    }
  })();
  return loading;
}
