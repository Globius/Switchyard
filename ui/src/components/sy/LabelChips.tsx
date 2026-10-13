/** The labels of an object as small chips; with `onPick`, a chip filters the list by that label. */
import { labelChips } from "@/lib/labels";
import { t } from "@/lib/i18n";

export function LabelChips({
  labels,
  onPick,
}: {
  labels: Record<string, string> | null | undefined;
  onPick?: (chip: string) => void;
}) {
  const chips = labelChips(labels);
  if (chips.length === 0) return null;
  return (
    <ul className="mt-1 flex flex-wrap gap-1" aria-label={t("Labels")}>
      {chips.map((c) => (
        <li key={c}>
          {onPick ? (
            <button
              type="button"
              title={t("Show only {label}", { label: c })}
              onClick={() => onPick(c)}
              className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground hover:bg-accent"
            >
              {c}
            </button>
          ) : (
            <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
              {c}
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}
