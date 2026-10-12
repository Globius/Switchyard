/**
 * A write-only secret field (account keys, EAB keys, DNS provider tokens). The value is never
 * shown again once stored: the field says "Set · stored in OpenBao" with a Replace button;
 * replacing shows an empty input and keeps the current value working until Save.
 */
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

export function WriteOnlySecret({
  label,
  isSet,
  value,
  onChange,
  multiline,
  disabled,
  help,
  required,
}: {
  label: string;
  /** True when a value is already stored. */
  isSet: boolean;
  /** What the user typed; empty means "keep" when a value is stored. */
  value: string;
  onChange: (v: string) => void;
  multiline?: boolean;
  disabled?: boolean;
  help?: string;
  required?: boolean;
}) {
  const [replacing, setReplacing] = useState(false);
  const editing = !isSet || replacing;
  const Field = multiline ? "textarea" : "input";
  return (
    <div className="space-y-1.5">
      <div className="text-sm font-medium">
        {isSet && replacing ? t("{label} (replacing)", { label }) : label}
        {required && !isSet && <span aria-hidden> *</span>}
      </div>
      {editing ? (
        <>
          <Field
            aria-label={label}
            autoComplete="off"
            spellCheck={false}
            {...(multiline ? { rows: 4 } : { type: "password" })}
            className={cn(INPUT, "font-mono", multiline && "h-24 py-2 text-xs")}
            disabled={disabled}
            value={value}
            onChange={(e) => onChange(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            {isSet
              ? t(
                  "Write-only. Saving stores the new value in OpenBao and discards the old one; until you save, the current value keeps working.",
                )
              : t("Write-only. Stored in OpenBao and never shown again.")}
          </p>
          {isSet && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => {
                onChange("");
                setReplacing(false);
              }}
            >
              {t("Keep the current value")}
            </Button>
          )}
        </>
      ) : (
        <div className="flex items-center gap-3">
          <Badge tone="healthy">{t("Set")}</Badge>
          <span className="font-mono text-xs text-muted-foreground">{t("stored in OpenBao")}</span>
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="ml-auto"
            disabled={disabled}
            aria-label={t("Replace {label}", { label })}
            onClick={() => setReplacing(true)}
          >
            {t("Replace")}
          </Button>
        </div>
      )}
      {help && <p className="text-xs text-muted-foreground">{help}</p>}
    </div>
  );
}
