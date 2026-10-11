import { ConfigEditor } from "@/components/sy/ConfigEditor";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import type { components } from "@/api/schema";

type Fragment = Omit<components["schemas"]["RenderedFragment"], "range"> & {
  range?: number[] | null;
};

/**
 * Read-only config with line numbers, HAProxy highlighting, the fragment's `range` highlighted and
 * the SE group's fingerprint. Monaco renders it in the browser; the plain view below is what shows
 * while Monaco loads and in tests.
 */
export function RenderedConfigView({ fragment }: { fragment: Fragment }) {
  const lines = fragment.content.replace(/\n$/, "").split("\n");
  const [from = 1, to = lines.length] = fragment.range ?? [];
  return (
    <figure className="overflow-hidden rounded-lg border bg-card">
      <figcaption className="flex items-center justify-between border-b bg-muted/60 px-4 py-2 text-xs">
        <span className="font-mono">{fragment.file}</span>
        <span className="text-muted-foreground">
          {t("config hash")} <span className="font-mono">{fragment.fingerprint.slice(0, 12)}</span>
        </span>
      </figcaption>
      <ConfigEditor
        content={fragment.content}
        range={
          fragment.range && fragment.range.length >= 2
            ? [fragment.range[0]!, fragment.range[1]!]
            : null
        }
        label={fragment.file}
        fallback={
          <pre className="overflow-x-auto py-2 font-mono text-[13px] leading-6">
            {lines.map((line, i) => {
              const n = i + 1;
              const hit = n >= from && n <= to;
              return (
                <div
                  key={n}
                  data-highlight={hit || undefined}
                  className={cn("flex px-4", hit && "bg-accent")}
                >
                  <span aria-hidden className="w-10 shrink-0 text-right pr-4 text-muted-foreground">
                    {n}
                  </span>
                  <code>{line || " "}</code>
                </div>
              );
            })}
          </pre>
        }
      />
    </figure>
  );
}
