// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Object labels (ADR 0198): `key` or `key=value` chips, and the `?label=` selectors the API takes. */

/** One label as the chip shown on screen: `key=value`, or just `key` when the value is empty. */
export function labelChips(labels: Record<string, string> | null | undefined): string[] {
  return Object.entries(labels ?? {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([k, v]) => (v ? `${k}=${v}` : k));
}

/** Back from chips to the object form. A later chip with the same key wins. */
export function chipsToLabels(chips: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const c of chips) {
    const [k = "", ...rest] = c.split("=");
    out[k] = rest.join("=");
  }
  return out;
}

const TOKEN = /^[a-z0-9]([a-z0-9_.-]{0,61}[a-z0-9])?$/;

/** The API's rule for a key and a value: a-z, 0-9, `-`, `_`, `.`, 1 to 63 characters. */
export function validateLabelChip(text: string): string | null {
  const [k = "", ...rest] = text.split("=");
  const v = rest.join("=");
  if (!TOKEN.test(k))
    return "Keys use a-z, 0-9, - _ . (up to 63, starting and ending with a letter or digit)";
  if (v !== "" && !TOKEN.test(v))
    return "Values use a-z, 0-9, - _ . (up to 63, starting and ending with a letter or digit)";
  return null;
}

/** Whether the API will accept this selector: `key`, `!key`, `key=value` or `key!=value`. */
export function isSelector(text: string): boolean {
  if (text.startsWith("!") && !text.includes("=")) return TOKEN.test(text.slice(1));
  const m = /^([^=!]+)(!?=)(.*)$/.exec(text);
  if (!m) return TOKEN.test(text);
  return TOKEN.test(m[1] ?? "") && (m[3] === "" || TOKEN.test(m[3] ?? ""));
}

/**
 * The text of a label filter box as selectors: comma or space separated. Parts the API would
 * refuse (half-typed ones, for example `Env`) are left out, so typing never shows an error.
 */
export function parseSelectors(text: string | undefined): string[] {
  return (text ?? "")
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter((s) => s !== "" && isSelector(s));
}
