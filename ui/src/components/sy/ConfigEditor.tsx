/**
 * Read-only Monaco view for configuration text: HAProxy highlighting for rendered files, diff
 * colouring for change-set diffs, an optional highlighted line range, and a plain fallback while
 * Monaco loads or where it cannot run.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { loadMonaco, type Monaco } from "@/lib/monaco";
import { useTheme } from "@/lib/theme";

const LINE = 19;

export function ConfigEditor({
  content,
  language = "haproxy",
  range,
  maxLines = 32,
  fallback,
  label,
}: {
  content: string;
  language?: "haproxy" | "sy-diff" | "plaintext";
  /** 1-based inclusive line range to highlight and scroll to. */
  range?: [number, number] | null;
  maxLines?: number;
  /** Plain rendering used until Monaco is ready, and where it is unavailable. */
  fallback: ReactNode;
  label: string;
}) {
  const host = useRef<HTMLDivElement>(null);
  const [monaco, setMonaco] = useState<Monaco | null>(null);
  const [ready, setReady] = useState(false);
  const { theme } = useTheme();
  const dark =
    theme === "dark" ||
    (theme === "system" &&
      typeof matchMedia !== "undefined" &&
      matchMedia("(prefers-color-scheme: dark)").matches);
  const lines = content.replace(/\n$/, "").split("\n").length;
  const height = Math.min(lines, maxLines) * LINE + 16;

  useEffect(() => {
    let off = false;
    void loadMonaco().then((m) => {
      if (!off) setMonaco(m);
    });
    return () => {
      off = true;
    };
  }, []);

  useEffect(() => {
    if (!monaco || !host.current) return;
    const editor = monaco.editor.create(host.current, {
      value: content.replace(/\n$/, ""),
      language,
      readOnly: true,
      theme: dark ? "sy-dark" : "sy-light",
      minimap: { enabled: false },
      scrollBeyondLastLine: false,
      automaticLayout: true,
      lineHeight: LINE,
      fontSize: 13,
      fontFamily: "'Geist Mono Variable', ui-monospace, monospace",
      renderLineHighlight: "none",
      folding: false,
      padding: { top: 8, bottom: 8 },
      ariaLabel: label,
      domReadOnly: true,
    });
    if (range) {
      editor.createDecorationsCollection([
        {
          range: new monaco.Range(range[0], 1, range[1], 1),
          options: { isWholeLine: true, className: "sy-range-highlight" },
        },
      ]);
      editor.revealLineInCenterIfOutsideViewport(range[0]);
    }
    setReady(true);
    return () => {
      editor.dispose();
      setReady(false);
    };
  }, [monaco, content, language, range, dark, label]);

  if (!monaco) return <>{fallback}</>;
  return (
    <>
      <div ref={host} style={{ height }} className="w-full" />
      {!ready && fallback}
    </>
  );
}
