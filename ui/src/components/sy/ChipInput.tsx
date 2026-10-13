// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * ChipInput: pick several items from a list (certificates, policy sets) or type free text where
 * the field allows it (SANs, groups).
 *
 * Keyboard: type to filter · ↑ ↓ move in the list · Enter or Tab adds the highlighted item ·
 * Backspace in an empty input selects the last chip, again removes it · ← → move between chips ·
 * Delete removes the focused chip · Esc closes the list. With `freeText`, Enter or comma adds the
 * typed text after `validate` accepts it. An error on one item marks that chip; an error on the
 * whole field marks the box.
 */
import { X } from "lucide-react";
import { useId, useMemo, useRef, useState } from "react";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export interface ChipOption {
  value: string;
  label: string;
  /** Right-aligned context in the list, for example "41 days". */
  hint?: string;
}

export function ChipInput({
  label,
  value,
  onChange,
  options,
  freeText,
  itemErrors,
  error,
  emptyMessage,
  placeholder,
  disabled,
}: {
  label: string;
  value: string[];
  onChange: (v: string[]) => void;
  options?: ChipOption[];
  /** Allow typed values; `validate` returns an error message, or null when the text is fine. */
  freeText?: { validate: (text: string) => string | null };
  /** Per-item error messages keyed by the item's value. */
  itemErrors?: Record<string, string>;
  /** Error on the whole field. */
  error?: string;
  /** Shown in the list when nothing matches. */
  emptyMessage?: (query: string) => string;
  placeholder?: string;
  disabled?: boolean;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const chips = useRef<(HTMLElement | null)[]>([]);
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [typed, setTyped] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);

  const labelOf = (v: string) => options?.find((o) => o.value === v)?.label ?? v;
  const matches = useMemo(
    () =>
      (options ?? []).filter(
        (o) => !value.includes(o.value) && o.label.toLowerCase().includes(q.trim().toLowerCase()),
      ),
    [options, value, q],
  );
  const showList = open && !!options && !disabled;

  function add(v: string) {
    if (!value.includes(v)) onChange([...value, v]);
    setQ("");
    setTyped(null);
    setActive(0);
  }
  function addTyped() {
    const text = q.trim().replace(/,$/, "");
    if (!text || !freeText) return;
    const problem = freeText.validate(text);
    if (problem) return setTyped(problem);
    add(text);
  }
  function removeAt(i: number) {
    const next = value.filter((_, j) => j !== i);
    onChange(next);
    const to = Math.min(i, next.length - 1);
    if (to >= 0) {
      setSelected(to);
      chips.current[to]?.focus();
    } else {
      setSelected(null);
      input.current?.focus();
    }
  }
  function focusChip(i: number) {
    setSelected(i);
    chips.current[i]?.focus();
  }

  function onInputKey(e: React.KeyboardEvent<HTMLInputElement>) {
    if (e.key === "ArrowDown" && options) {
      e.preventDefault();
      setOpen(true);
      setActive((a) => Math.min(a + 1, Math.max(matches.length - 1, 0)));
    } else if (e.key === "ArrowUp" && options) {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const m = matches[active];
      if (showList && m) add(m.value);
      else if (freeText) addTyped();
    } else if (e.key === "Tab" && showList && matches[active]) {
      e.preventDefault();
      add(matches[active]!.value);
    } else if (e.key === "," && freeText) {
      e.preventDefault();
      addTyped();
    } else if (e.key === "Escape") {
      if (open) e.stopPropagation();
      setOpen(false);
    } else if (e.key === "Backspace" && !q && value.length > 0) {
      e.preventDefault();
      focusChip(value.length - 1);
    } else if (e.key === "ArrowLeft" && input.current?.selectionStart === 0 && value.length > 0) {
      focusChip(value.length - 1);
    }
  }

  function onChipKey(e: React.KeyboardEvent<HTMLElement>, i: number) {
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (i > 0) focusChip(i - 1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      if (i < value.length - 1) focusChip(i + 1);
      else {
        setSelected(null);
        input.current?.focus();
      }
    } else if (e.key === "Delete" || e.key === "Backspace") {
      e.preventDefault();
      if (e.key === "Delete" || selected === i) removeAt(i);
    }
  }

  const fieldError = error ?? typed ?? undefined;
  return (
    <div className="space-y-1.5">
      <div
        className={cn(
          "flex min-h-9 flex-wrap items-center gap-1.5 rounded-md border bg-card px-2 py-1.5 focus-within:ring-2 focus-within:ring-ring",
          fieldError ? "border-status-down" : "border-input",
          disabled && "opacity-50",
        )}
        onClick={() => input.current?.focus()}
      >
        {value.map((v, i) => {
          const bad = itemErrors?.[v];
          return (
            <span
              key={v}
              ref={(el) => {
                chips.current[i] = el;
              }}
              role="group"
              tabIndex={-1}
              aria-label={bad ? `${labelOf(v)}: ${bad}` : labelOf(v)}
              aria-invalid={bad ? true : undefined}
              onKeyDown={(e) => onChipKey(e, i)}
              onFocus={() => setSelected(i)}
              className={cn(
                "inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium outline-none focus:ring-2 focus:ring-ring",
                bad
                  ? "bg-status-down-bg text-status-down"
                  : "bg-secondary text-secondary-foreground",
              )}
            >
              {labelOf(v)}
              <button
                type="button"
                tabIndex={-1}
                disabled={disabled}
                aria-label={t("Remove {name}", { name: labelOf(v) })}
                onClick={(e) => {
                  e.stopPropagation();
                  removeAt(i);
                }}
              >
                <X className="size-3" aria-hidden />
              </button>
            </span>
          );
        })}
        <input
          ref={input}
          role="combobox"
          aria-label={label}
          aria-expanded={showList}
          aria-controls={`${id}-list`}
          aria-autocomplete="list"
          aria-activedescendant={showList && matches[active] ? `${id}-o${active}` : undefined}
          aria-invalid={fieldError ? true : undefined}
          disabled={disabled}
          placeholder={value.length === 0 ? placeholder : undefined}
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setOpen(true);
            setActive(0);
            setTyped(null);
          }}
          onFocus={() => {
            setSelected(null);
            setOpen(true);
          }}
          onBlur={() => setOpen(false)}
          onKeyDown={onInputKey}
          className="min-w-24 flex-1 bg-transparent text-sm outline-none"
        />
      </div>
      {showList && (
        <ul
          id={`${id}-list`}
          role="listbox"
          aria-label={label}
          className="max-h-48 overflow-auto rounded-md border bg-popover p-1 text-sm shadow-md"
        >
          {matches.length === 0 ? (
            <li role="presentation" className="px-3 py-2 text-muted-foreground">
              {emptyMessage?.(q) ?? t("No matches")}
            </li>
          ) : (
            matches.map((o, i) => (
              <li
                key={o.value}
                id={`${id}-o${i}`}
                role="option"
                aria-selected={i === active}
                onMouseDown={(e) => {
                  e.preventDefault();
                  add(o.value);
                }}
                className={cn(
                  "flex cursor-pointer justify-between rounded px-3 py-1.5",
                  i === active && "bg-accent",
                )}
              >
                <span>{o.label}</span>
                {o.hint && <span className="text-xs text-muted-foreground">{o.hint}</span>}
              </li>
            ))
          )}
        </ul>
      )}
      {fieldError && (
        <p role="alert" className="text-xs text-status-down">
          {fieldError}
        </p>
      )}
      {itemErrors &&
        Object.entries(itemErrors)
          .filter(([v]) => value.includes(v))
          .map(([v, m]) => (
            <p key={v} role="alert" className="text-xs text-status-down">
              {labelOf(v)}: {m}
            </p>
          ))}
    </div>
  );
}

/** Hostname check for free-text SANs: labels of letters, digits and hyphens, optional `*.` prefix. */
export function validateHostname(text: string): string | null {
  const host = text.replace(/^\*\./, "");
  if (host.length > 253) return t("That name is too long.");
  const ok = host.split(".").every((l) => /^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/.test(l));
  return ok && host.includes(".") ? null : t("Enter a hostname such as docs.example.com.");
}
