/**
 * Single entry point for user-visible strings so i18n can be added later (03: English only for now).
 *
 * @example t("Retry") // "Retry"
 * @example t("{n} changes", { n: 3 }) // "3 changes"
 */
export function t(text: string, vars?: Record<string, string | number>): string {
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (m, k: string) => (k in vars ? String(vars[k]) : m));
}
