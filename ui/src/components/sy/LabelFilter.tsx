/** The "Label" filter box of the object lists: selectors, comma separated, kept in the URL. */
import { useState } from "react";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export function LabelFilter({
  value,
  onChange,
  className,
}: {
  value: string | undefined;
  onChange: (text: string | undefined) => void;
  className?: string;
}) {
  const [text, setText] = useState(value ?? "");
  const [seen, setSeen] = useState(value);
  // A chip click or a bookmarked URL changes the filter from outside the box.
  if (value !== seen) {
    setSeen(value);
    setText(value ?? "");
  }
  return (
    <input
      aria-label={t("Label")}
      title={t("Labels, comma separated: env=prod, team, !legacy, tier!=db")}
      placeholder={t("Label: env=prod")}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        onChange(e.target.value.trim() || undefined);
      }}
      className={cn(
        "h-9 w-44 rounded-md border border-input bg-card px-3 font-mono text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className,
      )}
    />
  );
}
