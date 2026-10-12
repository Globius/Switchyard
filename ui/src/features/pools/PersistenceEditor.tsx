/**
 * Pool persistence: how a client sticks to one member. Source IP, an HTTP cookie, a request
 * header, or the URI (path or a query parameter). The HTTP kinds are refused on tcp and udp
 * pools by the API; the finding shows on the pool.
 */
import type { components } from "@/api/schema";
import { Switch } from "@/components/ui/switch";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export type Persistence = NonNullable<components["schemas"]["PoolRead"]["persistence"]>;
type Kind = Persistence["type"] | "none";

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

const DEFAULTS: Record<Persistence["type"], Persistence> = {
  source_ip: { type: "source_ip", table_size: 100000, timeout_s: 1800 },
  http_cookie: {
    type: "http_cookie",
    name: "SYSRV",
    mode: "insert",
    http_only: true,
    secure: false,
    ttl_s: null,
  },
  header: { type: "header", header: "", table_size: 100000, timeout_s: 1800 },
  uri: { type: "uri", source: "path", param: null, table_size: 100000, timeout_s: 1800 },
};

export function PersistenceEditor({
  value,
  onChange,
  disabled,
}: {
  value: Persistence | null;
  onChange: (p: Persistence | null) => void;
  disabled?: boolean;
}) {
  const kind: Kind = value?.type ?? "none";
  const num = (v: string) => (v === "" ? 0 : Number(v));
  const field = "block space-y-1.5 text-sm font-medium";
  return (
    <fieldset className="space-y-3 rounded-lg border p-4">
      <legend className="px-1 text-sm font-medium">{t("Persistence")}</legend>
      <div className="grid gap-3 sm:grid-cols-3">
        <label className={field}>
          {t("Type")}
          <select
            aria-label={t("Persistence type")}
            className={INPUT}
            disabled={disabled}
            value={kind}
            onChange={(e) => {
              const k = e.target.value as Kind;
              onChange(k === "none" ? null : structuredClone(DEFAULTS[k]));
            }}
          >
            <option value="none">{t("None")}</option>
            <option value="source_ip">{t("Source IP")}</option>
            <option value="http_cookie">{t("HTTP cookie")}</option>
            <option value="header">{t("Request header")}</option>
            <option value="uri">{t("URI")}</option>
          </select>
        </label>
        {value && value.type !== "http_cookie" && (
          <>
            <label className={field}>
              {t("Table size")}
              <input
                aria-label={t("Table size")}
                type="number"
                className={INPUT}
                disabled={disabled}
                value={value.table_size ?? 100000}
                onChange={(e) => onChange({ ...value, table_size: num(e.target.value) })}
              />
            </label>
            <label className={field}>
              {t("Timeout (s)")}
              <input
                aria-label={t("Persistence timeout")}
                type="number"
                className={INPUT}
                disabled={disabled}
                value={value.timeout_s ?? 1800}
                onChange={(e) => onChange({ ...value, timeout_s: num(e.target.value) })}
              />
            </label>
          </>
        )}
      </div>
      {value?.type === "http_cookie" && (
        <div className="grid gap-3 sm:grid-cols-3">
          <label className={field}>
            {t("Cookie name")}
            <input
              aria-label={t("Cookie name")}
              className={cn(INPUT, "font-mono")}
              disabled={disabled}
              value={value.name ?? ""}
              onChange={(e) => onChange({ ...value, name: e.target.value })}
            />
          </label>
          <label className={field}>
            {t("Mode")}
            <select
              aria-label={t("Cookie mode")}
              className={INPUT}
              disabled={disabled}
              value={value.mode ?? "insert"}
              onChange={(e) =>
                onChange({ ...value, mode: e.target.value as "insert" | "rewrite" | "prefix" })
              }
            >
              <option value="insert">{t("Insert (HAProxy sets it)")}</option>
              <option value="rewrite">{t("Rewrite (the member sets it)")}</option>
              <option value="prefix">{t("Prefix the member's value")}</option>
            </select>
          </label>
          <label className={field}>
            {t("Lifetime (s)")}
            <input
              aria-label={t("Cookie lifetime")}
              type="number"
              min={1}
              className={INPUT}
              disabled={disabled || value.mode === "rewrite" || value.mode === "prefix"}
              placeholder={t("session")}
              value={value.ttl_s ?? ""}
              onChange={(e) =>
                onChange({ ...value, ttl_s: e.target.value ? Number(e.target.value) : null })
              }
            />
          </label>
          <label className="flex items-center gap-2 text-sm">
            <Switch
              aria-label={t("Secure")}
              checked={value.secure ?? false}
              disabled={disabled}
              onCheckedChange={(on) => onChange({ ...value, secure: on })}
            />
            {t("Secure")}
          </label>
          <label className="flex items-center gap-2 text-sm">
            <Switch
              aria-label={t("HttpOnly")}
              checked={value.http_only ?? true}
              disabled={disabled}
              onCheckedChange={(on) => onChange({ ...value, http_only: on })}
            />
            {t("HttpOnly")}
          </label>
        </div>
      )}
      {value?.type === "header" && (
        <label className={cn(field, "max-w-xs")}>
          {t("Header")}
          <input
            aria-label={t("Persistence header")}
            className={cn(INPUT, "font-mono")}
            disabled={disabled}
            placeholder="X-Session"
            value={value.header ?? ""}
            onChange={(e) => onChange({ ...value, header: e.target.value })}
          />
          <span className="block text-xs font-normal text-muted-foreground">
            {t("Requests without it fall back to the load-balancing algorithm.")}
          </span>
        </label>
      )}
      {value?.type === "uri" && (
        <div className="grid gap-3 sm:grid-cols-3">
          <label className={field}>
            {t("Stick on")}
            <select
              aria-label={t("URI source")}
              className={INPUT}
              disabled={disabled}
              value={value.source ?? "path"}
              onChange={(e) =>
                onChange({
                  ...value,
                  source: e.target.value as "path" | "url_param",
                  param: e.target.value === "path" ? null : (value.param ?? ""),
                })
              }
            >
              <option value="path">{t("The request path")}</option>
              <option value="url_param">{t("A query parameter")}</option>
            </select>
          </label>
          {value.source === "url_param" && (
            <label className={field}>
              {t("Parameter")}
              <input
                aria-label={t("URI parameter")}
                className={cn(INPUT, "font-mono")}
                disabled={disabled}
                value={value.param ?? ""}
                onChange={(e) => onChange({ ...value, param: e.target.value })}
              />
            </label>
          )}
        </div>
      )}
      {value && value.type !== "source_ip" && (
        <p className="text-xs text-muted-foreground">
          {t("Cookie, header and URI persistence work on HTTP pools only.")}
        </p>
      )}
    </fieldset>
  );
}
