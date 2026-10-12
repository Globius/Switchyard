// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Searchable single-select for object references (pools, monitors, certificates, SE groups, VIP
 * pools, IdP groups) and any list that can exceed ~10 items. Short fixed enums stay native
 * `<select>`s. The trigger looks like an Input; the list filters as you type; Up/Down move,
 * Enter picks, Escape closes.
 *
 * @example
 * <Combobox aria-label="Pool" value={id} onChange={setId} options={[{ value: "p1", label: "web" }]} />
 */
import { Check, ChevronDown } from "lucide-react";
import { Popover } from "radix-ui";
import { useId, useMemo, useState } from "react";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export interface ComboOption {
  value: string;
  label: string;
}

export function Combobox({
  value,
  onChange,
  options,
  placeholder,
  disabled,
  className,
  "aria-label": ariaLabel,
}: {
  value: string;
  onChange: (value: string) => void;
  options: ComboOption[];
  /** Shown while nothing matches `value`. */
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  "aria-label": string;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const list = useId();
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return needle ? options.filter((o) => o.label.toLowerCase().includes(needle)) : options;
  }, [options, q]);
  const current = options.find((o) => o.value === value);

  function pick(o: ComboOption) {
    onChange(o.value);
    setOpen(false);
  }

  return (
    <Popover.Root
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        setQ("");
        setActive(
          Math.max(
            0,
            options.findIndex((x) => x.value === value),
          ),
        );
      }}
    >
      <Popover.Trigger asChild>
        <button
          type="button"
          role="combobox"
          aria-label={ariaLabel}
          aria-expanded={open}
          aria-controls={open ? list : undefined}
          disabled={disabled}
          className={cn(
            "flex h-9 w-full items-center justify-between gap-2 rounded-md border border-input bg-card px-3 text-left text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-60",
            className,
          )}
        >
          <span className={cn("truncate", !current && "text-muted-foreground")}>
            {current?.label ?? placeholder ?? t("Choose…")}
          </span>
          <ChevronDown className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="start"
          sideOffset={4}
          className="z-[60] w-(--radix-popover-trigger-width) min-w-56 rounded-lg border bg-popover p-1 text-popover-foreground shadow-lg"
        >
          <input
            autoFocus
            aria-label={t("Search")}
            placeholder={t("Search…")}
            className="mb-1 h-8 w-full rounded-md border border-input bg-card px-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
            value={q}
            onChange={(e) => {
              setQ(e.target.value);
              setActive(0);
            }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setActive((a) => Math.min(a + 1, shown.length - 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setActive((a) => Math.max(a - 1, 0));
              } else if (e.key === "Enter") {
                e.preventDefault();
                if (shown[active]) pick(shown[active]);
              }
            }}
          />
          <ul id={list} role="listbox" aria-label={ariaLabel} className="max-h-64 overflow-auto">
            {shown.length === 0 && (
              <li className="px-2 py-1.5 text-sm text-muted-foreground">{t("No matches")}</li>
            )}
            {shown.map((o, i) => (
              <li
                key={o.value}
                role="option"
                aria-selected={o.value === value}
                data-value={o.value}
                className={cn(
                  "flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm",
                  i === active && "bg-accent",
                )}
                onMouseEnter={() => setActive(i)}
                onClick={() => pick(o)}
              >
                <Check
                  className={cn("size-4 shrink-0", o.value !== value && "invisible")}
                  aria-hidden
                />
                <span className="truncate">{o.label}</span>
              </li>
            ))}
          </ul>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
