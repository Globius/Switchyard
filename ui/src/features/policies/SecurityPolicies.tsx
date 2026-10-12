// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Editors for the security policy kinds: Access (login through the tenant's identity provider,
 * ADR 0130 to 0132), JWT bearer tokens (ADR 0135), client certificate identity (ADR 0136), WAF
 * (Coraza with the OWASP Core Rule Set, ADR 0140) and bot and DDoS protection (ADR 0150).
 * Secrets are never typed here: the forms take OpenBao paths and public keys only.
 */
import { X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { ChipInput } from "@/components/sy/ChipInput";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { ErrorState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { useSaveOp } from "@/features/objects/save";
import { t } from "@/lib/i18n";
import { cidrProblem } from "@/lib/ip";
import { cn } from "@/lib/utils";
import { cleanMatch, emptyMatch, MatchEditor, type Match } from "./MatchEditor";

export type SecurityKind = "access" | "jwt" | "client_cert" | "waf" | "protection";
export type SecurityPolicy =
  | components["schemas"]["AccessPolicySetRead"]
  | components["schemas"]["JwtPolicySetRead"]
  | components["schemas"]["ClientCertPolicySetRead"]
  | components["schemas"]["WafPolicySetRead"]
  | components["schemas"]["ProtectionPolicySetRead"];

export const SECURITY_LABEL: Record<SecurityKind, string> = {
  access: "Login",
  jwt: "JWT tokens",
  client_cert: "Client certificate",
  waf: "WAF",
  protection: "Bot and DDoS protection",
};

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";
const MONO = cn(INPUT, "font-mono");
type Obj = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

function Field({ label, help, children }: { label: string; help?: string; children: ReactNode }) {
  return (
    <label className="block space-y-1.5 text-sm font-medium">
      {label}
      {children}
      {help && <span className="block text-xs font-normal text-muted-foreground">{help}</span>}
    </label>
  );
}

function Rule({
  title,
  onRemove,
  disabled,
  children,
}: {
  title: string;
  onRemove: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="space-y-3 rounded-lg border p-4 text-sm">
      <div className="flex items-center">
        <span className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">
          {title}
        </span>
        <Button
          size="sm"
          variant="ghost"
          className="ml-auto"
          disabled={disabled}
          aria-label={`${t("Remove")} ${title}`}
          onClick={onRemove}
        >
          <X className="size-4" />
        </Button>
      </div>
      {children}
    </div>
  );
}

/** The default configuration of a new set of each kind (the part after `kind` and `name`). */
export function defaults(kind: SecurityKind): Obj {
  switch (kind) {
    case "access":
      return {
        oidc: {
          issuer: "",
          client_id: "",
          client_secret_ref: "",
          cookie_secret_ref: "",
          groups_claim: "groups",
          scopes: ["openid", "email", "profile"],
          cookie_name: "_sy_auth",
          cookie_expire_s: 28800,
          cookie_refresh_s: 900,
        },
        default_mode: "login",
        auth_path: "/oauth2",
        identity_headers: {
          user: "X-Auth-User",
          email: "X-Auth-Email",
          groups: "X-Auth-Groups",
          token: "",
        },
        rules: [],
      };
    case "jwt":
      return {
        rules: [
          {
            match: emptyMatch(),
            issuer: "",
            audiences: [],
            required_scopes: [],
            leeway_s: 30,
            subject_header: "",
            jwks_url: "",
            keys: [{ kid: "", alg: "RS256", public_key_pem: "" }],
          },
        ],
      };
    case "client_cert":
      return {
        user_header: "X-Cert-User",
        groups_header: "X-Cert-Groups",
        identities: [],
        rules: [{ match: emptyMatch(), mode: "valid", groups: [] }],
      };
    case "waf":
      return {
        mode: "detect",
        fail_mode: "open",
        paranoia_level: 1,
        inbound_threshold: 5,
        outbound_threshold: 4,
        inspect_body: true,
        timeout_ms: 100,
        disabled_rule_ids: [],
        excluded_paths: [],
        crs_version: "4.8.0",
      };
    case "protection":
      return {
        max_connections_per_source: null,
        max_new_connections_per_source: null,
        connection_rate_period_s: 10,
        header_timeout_s: null,
        client_timeout_s: null,
        table_size: 100000,
        block_empty_user_agent: false,
        block_scanners: false,
        deny_user_agents: [],
        bot_action: "deny",
        exempt_cidrs: [],
        challenge: null,
        captcha: null,
      };
  }
}

const nz = (v: unknown) => (v === "" || v === undefined ? null : v);

const GATE_DEFAULT = {
  valid_for_s: 3600,
  skip_paths: [] as string[],
  skip_user_agents: [] as string[],
};

type Claim = {
  name: string;
  op: "present" | "true" | "equals" | "in";
  value?: string | null;
  values: string[];
};

/** Device conditions of an access rule besides the login (ADR 0138). */
export function PostureEditor({
  id,
  posture,
  disabled,
  onChange,
}: {
  id: string;
  posture: { device_certificate: boolean; claims: Claim[] } | null;
  disabled: boolean;
  onChange: (p: { device_certificate: boolean; claims: Claim[] } | null) => void;
}) {
  const p = posture ?? { device_certificate: false, claims: [] };
  const emit = (n: typeof p) => onChange(n.device_certificate || n.claims.length > 0 ? n : null);
  const setClaim = (i: number, c: Partial<Claim>) =>
    emit({ ...p, claims: p.claims.map((x, j) => (j === i ? { ...x, ...c } : x)) });
  return (
    <fieldset className="space-y-2 rounded-md border border-dashed p-3">
      <legend className="px-1 text-xs font-medium">{t("Device conditions")}</legend>
      <label className="flex items-center gap-2 text-sm">
        <Switch
          aria-label={`${id} requires a device certificate`}
          checked={p.device_certificate}
          disabled={disabled}
          onCheckedChange={(v) => emit({ ...p, device_certificate: v })}
        />
        {t("A client certificate the port accepts")}
      </label>
      {p.device_certificate && (
        <p className="text-xs text-muted-foreground">
          {t(
            "The service needs a port with a client CA; use Verify: optional so other paths still work.",
          )}
        </p>
      )}
      {p.claims.map((c, i) => (
        <div key={i} className="grid grid-cols-[1fr_8rem_1fr_auto] items-start gap-2">
          <input
            aria-label={`${id} claim ${i + 1} name`}
            placeholder="device_compliant"
            className={MONO}
            disabled={disabled}
            value={c.name}
            onChange={(e) => setClaim(i, { name: e.target.value })}
          />
          <select
            aria-label={`${id} claim ${i + 1} check`}
            className={INPUT}
            disabled={disabled}
            value={c.op}
            onChange={(e) => setClaim(i, { op: e.target.value as Claim["op"] })}
          >
            <option value="present">{t("is present")}</option>
            <option value="true">{t("is true")}</option>
            <option value="equals">{t("equals")}</option>
            <option value="in">{t("is one of")}</option>
          </select>
          {c.op === "equals" ? (
            <input
              aria-label={`${id} claim ${i + 1} value`}
              className={INPUT}
              disabled={disabled}
              value={c.value ?? ""}
              onChange={(e) => setClaim(i, { value: e.target.value })}
            />
          ) : c.op === "in" ? (
            <ChipInput
              label={`${id} claim ${i + 1} values`}
              value={c.values}
              onChange={(v) => setClaim(i, { values: v })}
              freeText={{ validate: () => null }}
              disabled={disabled}
            />
          ) : (
            <span />
          )}
          <Button
            size="sm"
            variant="ghost"
            disabled={disabled}
            aria-label={`${id} remove claim ${i + 1}`}
            onClick={() => emit({ ...p, claims: p.claims.filter((_, j) => j !== i) })}
          >
            <X className="size-4" />
          </Button>
        </div>
      ))}
      <Button
        size="sm"
        variant="outline"
        disabled={disabled}
        onClick={() =>
          emit({ ...p, claims: [...p.claims, { name: "", op: "true", value: null, values: [] }] })
        }
      >
        {t("Add claim check")}
      </Button>
    </fieldset>
  );
}

/** "Check new visitors": nothing, a JavaScript proof of work, or a CAPTCHA (ADR 0171, 0172). */
function NewVisitorCheck({
  cfg,
  set,
  disabled,
}: {
  cfg: Obj;
  set: (p: Obj) => void;
  disabled: boolean;
}) {
  const mode: "none" | "js" | "captcha" = cfg.challenge ? "js" : cfg.captcha ? "captcha" : "none";
  const gate: Obj = cfg.challenge ?? cfg.captcha;
  const key = mode === "js" ? "challenge" : "captcha";
  const setGate = (p: Obj) => set({ [key]: { ...gate, ...p } });
  return (
    <fieldset className="space-y-3 rounded-lg border p-4">
      <legend className="px-1 text-sm font-medium">{t("Check new visitors")}</legend>
      <Field
        label={t("Before the first answer")}
        help={t("A challenge or a CAPTCHA, not both. Passed visitors get a signed cookie.")}
      >
        <select
          aria-label={t("Visitor check")}
          className={INPUT}
          disabled={disabled}
          value={mode}
          onChange={(e) =>
            set(
              e.target.value === "js"
                ? { challenge: { ...GATE_DEFAULT, difficulty: 4 }, captcha: null }
                : e.target.value === "captcha"
                  ? {
                      captcha: {
                        ...GATE_DEFAULT,
                        provider: "turnstile",
                        site_key: "",
                        secret_ref: "",
                      },
                      challenge: null,
                    }
                  : { challenge: null, captcha: null },
            )
          }
        >
          <option value="none">{t("Nothing")}</option>
          <option value="js">{t("JavaScript challenge (proof of work)")}</option>
          <option value="captcha">{t("CAPTCHA")}</option>
        </select>
      </Field>
      {mode === "js" && (
        <Field
          label={t("Difficulty")}
          help={t("3 is about 4,000 hashes, 4 about 65,000, 5 about a million.")}
        >
          <select
            aria-label={t("Challenge difficulty")}
            className={INPUT}
            disabled={disabled}
            value={gate.difficulty}
            onChange={(e) => setGate({ difficulty: Number(e.target.value) })}
          >
            {[3, 4, 5].map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </Field>
      )}
      {mode === "captcha" && (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("Provider")}>
            <select
              aria-label={t("CAPTCHA provider")}
              className={INPUT}
              disabled={disabled}
              value={gate.provider}
              onChange={(e) => setGate({ provider: e.target.value })}
            >
              <option value="turnstile">Cloudflare Turnstile</option>
              <option value="hcaptcha">hCaptcha</option>
            </select>
          </Field>
          <Field label={t("Site key")}>
            <input
              aria-label={t("CAPTCHA site key")}
              className={MONO}
              disabled={disabled}
              value={gate.site_key}
              onChange={(e) => setGate({ site_key: e.target.value })}
            />
          </Field>
          <Field
            label={t("Secret key (OpenBao path)")}
            help={t("The path of the provider's secret key, never the key itself.")}
          >
            <input
              aria-label={t("CAPTCHA secret reference")}
              className={MONO}
              disabled={disabled}
              placeholder="tenants/acme/captcha"
              value={gate.secret_ref}
              onChange={(e) => setGate({ secret_ref: e.target.value })}
            />
          </Field>
        </div>
      )}
      {mode !== "none" && (
        <>
          <Field label={t("A pass lasts (s)")}>
            <input
              aria-label={t("Pass lifetime")}
              type="number"
              min={300}
              max={86400}
              className={INPUT}
              disabled={disabled}
              value={gate.valid_for_s}
              onChange={(e) => setGate({ valid_for_s: Number(e.target.value) })}
            />
          </Field>
          <ChipInput
            label={t("Paths never checked")}
            value={gate.skip_paths}
            onChange={(v) => setGate({ skip_paths: v })}
            freeText={{ validate: (v) => (v.startsWith("/") ? null : t("Starts with /")) }}
            placeholder="/api"
            disabled={disabled}
          />
          <ChipInput
            label={t("User-Agent text never checked")}
            value={gate.skip_user_agents}
            onChange={(v) => setGate({ skip_user_agents: v.map((x) => x.toLowerCase()) })}
            freeText={{ validate: () => null }}
            placeholder="googlebot"
            disabled={disabled}
          />
        </>
      )}
    </fieldset>
  );
}

type JwtKey = { kid: string; alg: string; public_key_pem: string };

const b64 = (buf: ArrayBuffer) =>
  btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/(.{64})/g, "$1\n");

/**
 * Converts a JWKS (JSON) into public-key PEM entries in the browser (WebCrypto). Private keys are
 * never kept; entries without a usable public key are skipped with the reason.
 */
export async function jwksToKeys(
  text: string,
  existing: JwtKey[],
): Promise<{ keys: JwtKey[]; added: number; already: number; skipped: string[] }> {
  const doc = JSON.parse(text) as { keys?: Record<string, unknown>[] };
  const list = Array.isArray(doc.keys) ? doc.keys : [];
  const keys = existing.filter((k) => k.public_key_pem.trim() !== "");
  let added = 0;
  let already = 0;
  const skipped: string[] = [];
  for (const j of list) {
    const kid = typeof j.kid === "string" ? j.kid : "";
    if (kid && keys.some((k) => k.kid === kid)) {
      already += 1;
      continue;
    }
    try {
      const pub = { ...j };
      delete pub.d;
      delete pub.p;
      delete pub.q;
      delete pub.dp;
      delete pub.dq;
      delete pub.qi;
      const rsa = j.kty === "RSA";
      const ec = j.kty === "EC";
      if (!rsa && !ec) throw new Error(`key type ${String(j.kty)}`);
      const key = await globalThis.crypto.subtle.importKey(
        "jwk",
        pub as JsonWebKey,
        rsa
          ? { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }
          : { name: "ECDSA", namedCurve: String(j.crv) },
        true,
        ["verify"],
      );
      const der = await globalThis.crypto.subtle.exportKey("spki", key);
      const alg =
        typeof j.alg === "string"
          ? j.alg
          : rsa
            ? "RS256"
            : j.crv === "P-384"
              ? "ES384"
              : j.crv === "P-521"
                ? "ES512"
                : "ES256";
      keys.push({
        kid,
        alg,
        public_key_pem: `-----BEGIN PUBLIC KEY-----\n${b64(der)}\n-----END PUBLIC KEY-----`,
      });
      added += 1;
    } catch (e) {
      skipped.push(`${kid || "(no kid)"}: ${e instanceof Error ? e.message : "unusable key"}`);
    }
  }
  return { keys, added, already, skipped };
}

function JwksImport({
  id,
  keys,
  onKeys,
  disabled,
}: {
  id: string;
  keys: JwtKey[];
  onKeys: (k: JwtKey[]) => void;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [result, setResult] = useState<string | null>(null);
  const run = async () => {
    try {
      const r = await jwksToKeys(text, keys);
      onKeys(r.keys.length > 0 ? r.keys.slice(0, 8) : keys);
      setResult(
        t("{a} added, {b} already here, {c} skipped", {
          a: r.added,
          b: r.already,
          c: r.skipped.length,
        }) + (r.skipped.length ? `: ${r.skipped.join("; ")}` : ""),
      );
      setText("");
    } catch {
      setResult(t("That is not valid JSON."));
    }
  };
  return (
    <div className="space-y-1.5">
      <Button size="sm" variant="outline" disabled={disabled} onClick={() => setOpen(!open)}>
        {t("Import from a JWKS")}
      </Button>
      {open && (
        <>
          <textarea
            aria-label={`${id} JWKS`}
            className={cn(MONO, "h-24 py-2 text-xs")}
            placeholder='{"keys":[…]}'
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <Button size="sm" disabled={disabled || !text.trim()} onClick={() => void run()}>
            {t("Convert and add")}
          </Button>
        </>
      )}
      {result && (
        <p role="status" className="text-xs text-muted-foreground">
          {result}
        </p>
      )}
    </div>
  );
}

type CertIdentity = { by: "cn" | "sha1"; value: string; user: string; groups: string[] };

/**
 * Parses `by,value,user,groups` lines (groups separated by `;`; a header line is skipped) into
 * known-certificate entries, up to 500 in all, reporting each bad line.
 */
export function parseCertCsv(
  text: string,
  existing: CertIdentity[],
): { identities: CertIdentity[]; added: number; errors: { line: number; message: string }[] } {
  const identities = [...existing];
  const errors: { line: number; message: string }[] = [];
  let added = 0;
  text.split(/\r?\n/).forEach((raw, i) => {
    const line = raw.trim();
    if (!line || (i === 0 && /^by\s*,/i.test(line))) return;
    const [by, value, user, groups = ""] = line.split(",").map((x) => x.trim());
    const fail = (message: string) => errors.push({ line: i + 1, message });
    if (by !== "cn" && by !== "sha1") return fail(t("The first column is cn or sha1."));
    if (!value) return fail(t("The value is empty."));
    if (by === "sha1" && !/^[0-9a-fA-F]{40}$/.test(value))
      return fail(t("A SHA-1 fingerprint is 40 hex digits."));
    if (!user) return fail(t("The user is empty."));
    if (identities.some((x) => x.by === by && x.value.toLowerCase() === value.toLowerCase()))
      return fail(t("Already in the list."));
    if (identities.length >= 500) return fail(t("The list holds at most 500 certificates."));
    identities.push({
      by,
      value,
      user,
      groups: groups
        .split(";")
        .map((g) => g.trim())
        .filter(Boolean),
    });
    added += 1;
  });
  return { identities, added, errors };
}

function CertCsvImport({
  identities,
  onIdentities,
  disabled,
}: {
  identities: CertIdentity[];
  onIdentities: (l: CertIdentity[]) => void;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [result, setResult] = useState<ReturnType<typeof parseCertCsv> | null>(null);
  return (
    <div className="space-y-1.5">
      <Button size="sm" variant="outline" disabled={disabled} onClick={() => setOpen(!open)}>
        {t("Import CSV")}
      </Button>
      {open && (
        <>
          <textarea
            aria-label={t("Certificates CSV")}
            className={cn(MONO, "h-28 py-2 text-xs")}
            placeholder={"by,value,user,groups\ncn,sam,sam.o,ops;dev"}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <Button
            size="sm"
            disabled={disabled || !text.trim()}
            onClick={() => {
              const r = parseCertCsv(text, identities);
              onIdentities(r.identities.filter((x) => x.value !== "" || x.user !== ""));
              setResult(r);
              if (r.errors.length === 0) setText("");
            }}
          >
            {t("Add from CSV")}
          </Button>
        </>
      )}
      {result && (
        <div role="status" className="space-y-0.5 text-xs">
          <p className="text-muted-foreground">
            {t("{n} added, {e} lines with errors", { n: result.added, e: result.errors.length })}
          </p>
          {result.errors.map((e) => (
            <p key={e.line} className="text-status-down">
              {t("Line {n}: {m}", { n: e.line, m: e.message })}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

/** An OpenBao path typed after the tenant's fixed `kv/tenants/<tenant>/` prefix. */
export function SecretPath({
  label,
  tenant,
  value,
  onChange,
  disabled,
}: {
  label: string;
  tenant: string;
  value: string;
  onChange: (v: string) => void;
  disabled: boolean;
}) {
  const prefix = `kv/tenants/${tenant}/`;
  const own = value === "" || value.startsWith(prefix);
  return (
    <div className="flex items-center">
      {own && (
        <span className="flex h-9 shrink-0 items-center rounded-l-md border border-r-0 bg-muted px-2 font-mono text-xs text-muted-foreground">
          {prefix}
        </span>
      )}
      <input
        aria-label={label}
        className={cn(MONO, own && "rounded-l-none")}
        disabled={disabled}
        value={own ? value.slice(prefix.length) : value}
        onChange={(e) => onChange(own ? prefix + e.target.value : e.target.value)}
      />
    </div>
  );
}

/** Where to put the secrets a login policy points at (the API never takes a secret). */
function PutSecretCard({ tenant }: { tenant: string }) {
  return (
    <div className="rounded-lg border border-dashed p-3 text-xs text-muted-foreground sm:col-span-2">
      <div className="mb-1 text-sm font-medium text-foreground">{t("Put a secret")}</div>
      {t("Store the secret in OpenBao yourself, then type its path above. For example:")}
      <pre className="mt-1 overflow-x-auto rounded bg-muted p-2 font-mono">
        {`bao kv put kv/tenants/${tenant}/oidc client_secret=…`}
      </pre>
    </div>
  );
}

/** The body the API takes: empty optional values become null and empty matches are trimmed. */
export function securityBody(kind: SecurityKind, name: string, cfg: Obj): Obj {
  const c = structuredClone(cfg);
  if (kind === "access") {
    for (const k of Object.keys(c.identity_headers))
      c.identity_headers[k] = nz(c.identity_headers[k]);
    c.oidc = { ...c.oidc, provider_ca_pem: nz(c.oidc.provider_ca_pem) };
    c.rules = c.rules.map((r: Obj) => ({
      ...r,
      match: cleanMatch(r.match),
      posture:
        r.mode === "public" || !r.posture
          ? null
          : {
              ...r.posture,
              claims: r.posture.claims.map((k: Obj) => ({
                name: k.name,
                op: k.op,
                value: k.op === "equals" ? k.value || null : null,
                values: k.op === "in" ? k.values : [],
              })),
            },
    }));
  }
  if (kind === "jwt")
    c.rules = c.rules.map((r: Obj) => ({
      ...r,
      match: cleanMatch(r.match),
      subject_header: nz(r.subject_header),
      jwks_url: nz(r.jwks_url),
      keys: r.keys.map((k: Obj) => ({ ...k, kid: nz(k.kid) })),
    }));
  if (kind === "client_cert") {
    c.rules = c.rules.map((r: Obj) => ({ ...r, match: cleanMatch(r.match) }));
    c.user_header = nz(c.user_header);
    c.groups_header = nz(c.groups_header);
  }
  return { kind, name, ...c };
}

function Nums({
  v,
  set,
  min,
  max,
  label,
}: {
  v: number | null;
  set: (n: number | null) => void;
  min?: number;
  max?: number;
  label: string;
}) {
  return (
    <input
      aria-label={label}
      type="number"
      min={min}
      max={max}
      className={INPUT}
      value={v ?? ""}
      onChange={(e) => set(e.target.value === "" ? null : Number(e.target.value))}
    />
  );
}

export function SecurityPolicySheet({
  tenant,
  policy,
  kind,
  usedBy,
  onClose,
}: {
  tenant: string;
  policy: SecurityPolicy | null;
  kind: SecurityKind;
  usedBy: string[];
  onClose: () => void;
}) {
  const save = useSaveOp(tenant, "policy_set");
  const write = useCan(tenant)("write", "application");
  const dis = !write.allowed;
  const [name, setName] = useState(policy?.name ?? "");
  const [cfg, setCfg] = useState<Obj>(() => {
    if (!policy) return defaults(kind);
    const skip = new Set([
      "id",
      "tenant_id",
      "version",
      "name",
      "kind",
      "changed_fields",
      "draft_status",
    ]);
    return structuredClone(
      Object.fromEntries(Object.entries(policy).filter(([k]) => !skip.has(k))),
    );
  });
  const [problem, setProblem] = useState<unknown>(null);
  const [errors, setErrors] = useState<{ path: string; message: string }[]>([]);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const set = (p: Obj) => setCfg({ ...cfg, ...p });
  const oidc = (p: Obj) => set({ oidc: { ...cfg.oidc, ...p } });
  const rows = <T,>(key: string, update: (list: T[]) => T[]) =>
    set({ [key]: update(cfg[key] as T[]) });

  async function submit() {
    setProblem(null);
    setErrors([]);
    const body = securityBody(kind, name, cfg);
    try {
      if (!policy) await save.mutateAsync({ action: "create", body });
      else
        await save.mutateAsync({ action: "update", id: policy.id, version: policy.version, body });
      onClose();
    } catch (e) {
      if (e instanceof ApiError && e.status === 422)
        setErrors((e.problem?.errors ?? []).map((x) => ({ path: x.path, message: x.message })));
      else setProblem(e);
    }
  }

  const crs = useQuery({
    queryKey: ["waf", "crs-rules"],
    enabled: kind === "waf",
    staleTime: 3_600_000,
    queryFn: async () => unwrap(await api.GET("/api/v1/waf/crs-rules")),
  });
  const crsOptions = (crs.data?.groups ?? []).flatMap((g) =>
    g.rules.map((r) => ({
      value: String(r.id),
      label: `${r.id} ${r.name}`,
      hint: `${g.name} · PL${r.paranoia_level}`,
    })),
  );
  const exempt = ((cfg.exempt_cidrs ?? []) as string[]).join("\n");
  const exemptBad = (cfg.exempt_cidrs ?? []).some((c: string) => cidrProblem(c));

  return (
    <section
      aria-label={
        policy ? policy.name : t("New {kind}", { kind: t(SECURITY_LABEL[kind]).toLowerCase() })
      }
      className="flex min-h-[28rem] flex-col rounded-lg border bg-card"
    >
      <div className="flex items-center gap-3 border-b px-5 py-3">
        <h2 className="text-base font-semibold">
          {policy
            ? t("Edit {name}", { name: policy.name })
            : t("New {kind}", { kind: t(SECURITY_LABEL[kind]).toLowerCase() })}
        </h2>
        <span className="text-xs text-muted-foreground">{t(SECURITY_LABEL[kind])}</span>
        <button
          type="button"
          className="ml-auto rounded-md p-1 hover:bg-accent"
          aria-label={t("Close")}
          onClick={onClose}
        >
          <X className="size-4" />
        </button>
      </div>
      <div className="flex-1 space-y-5 p-5">
        {problem != null && <ErrorState error={problem} />}
        {errors.length > 0 && (
          <ul
            role="alert"
            className="space-y-1 rounded-md border border-status-down/40 bg-status-down-bg p-3 text-xs"
          >
            {errors.map((e) => (
              <li key={e.path + e.message}>
                <span className="font-mono">{e.path}</span>: {e.message}
              </li>
            ))}
          </ul>
        )}
        {usedBy.length > 0 && (
          <p className="text-xs text-muted-foreground">
            {t("Used by {names}", { names: usedBy.join(", ") })}
          </p>
        )}
        <div className="max-w-sm">
          <Field
            label={t("Name")}
            help={
              policy
                ? t(
                    "Renaming is a draft change. Virtual services attach policies by ID, so they keep working.",
                  )
                : undefined
            }
          >
            <input
              aria-label={t("Name")}
              className={INPUT}
              disabled={dis}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
        </div>

        {kind === "access" && (
          <>
            <fieldset className="space-y-3 rounded-lg border p-4">
              <legend className="px-1 text-sm font-medium">
                {t("Identity provider (OpenID Connect)")}
              </legend>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label={t("Issuer URL")} help={t("https; discovery is read from it.")}>
                  <input
                    aria-label={t("Issuer URL")}
                    className={MONO}
                    disabled={dis}
                    value={cfg.oidc.issuer}
                    onChange={(e) => oidc({ issuer: e.target.value })}
                  />
                </Field>
                <Field label={t("Client ID")}>
                  <input
                    aria-label={t("Client ID")}
                    className={MONO}
                    disabled={dis}
                    value={cfg.oidc.client_id}
                    onChange={(e) => oidc({ client_id: e.target.value })}
                  />
                </Field>
                <Field
                  label={t("Client secret (OpenBao path)")}
                  help={t(
                    "Type the rest of the path after the fixed prefix; never the secret itself.",
                  )}
                >
                  <SecretPath
                    label={t("Client secret reference")}
                    tenant={tenant}
                    disabled={dis}
                    value={cfg.oidc.client_secret_ref}
                    onChange={(v) => oidc({ client_secret_ref: v })}
                  />
                </Field>
                <Field label={t("Cookie secret (OpenBao path)")}>
                  <SecretPath
                    label={t("Cookie secret reference")}
                    tenant={tenant}
                    disabled={dis}
                    value={cfg.oidc.cookie_secret_ref}
                    onChange={(v) => oidc({ cookie_secret_ref: v })}
                  />
                </Field>
                <PutSecretCard tenant={tenant} />
                <div className="sm:col-span-2">
                  <Field
                    label={t("Provider CA (PEM, optional)")}
                    help={t(
                      "Only when the provider's TLS certificate is signed by a private CA. Public certificates, not keys.",
                    )}
                  >
                    <textarea
                      aria-label={t("Provider CA")}
                      className={cn(MONO, "h-24 py-2 text-xs")}
                      placeholder="-----BEGIN CERTIFICATE-----"
                      disabled={dis}
                      value={cfg.oidc.provider_ca_pem ?? ""}
                      onChange={(e) => oidc({ provider_ca_pem: e.target.value })}
                    />
                  </Field>
                </div>
                <Field label={t("Groups claim")}>
                  <input
                    aria-label={t("Groups claim")}
                    className={MONO}
                    disabled={dis}
                    value={cfg.oidc.groups_claim}
                    onChange={(e) => oidc({ groups_claim: e.target.value })}
                  />
                </Field>
                <Field label={t("Cookie name")}>
                  <input
                    aria-label={t("Cookie name")}
                    className={MONO}
                    disabled={dis}
                    value={cfg.oidc.cookie_name}
                    onChange={(e) => oidc({ cookie_name: e.target.value })}
                  />
                </Field>
                <Field label={t("Session lifetime (s)")}>
                  <Nums
                    label={t("Session lifetime")}
                    v={cfg.oidc.cookie_expire_s}
                    set={(n) => oidc({ cookie_expire_s: n ?? 28800 })}
                    min={60}
                  />
                </Field>
                <Field label={t("Refresh every (s)")}>
                  <Nums
                    label={t("Session refresh")}
                    v={cfg.oidc.cookie_refresh_s}
                    set={(n) => oidc({ cookie_refresh_s: n ?? 900 })}
                    min={60}
                  />
                </Field>
              </div>
              <div className="space-y-1.5">
                <div className="text-sm font-medium">{t("Scopes")}</div>
                <ChipInput
                  label={t("Scopes")}
                  value={cfg.oidc.scopes}
                  onChange={(v) => oidc({ scopes: v })}
                  freeText={{ validate: () => null }}
                  disabled={dis}
                />
              </div>
            </fieldset>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label={t("Requests without a matching rule")}>
                <select
                  aria-label={t("Default mode")}
                  className={INPUT}
                  disabled={dis}
                  value={cfg.default_mode}
                  onChange={(e) => set({ default_mode: e.target.value })}
                >
                  <option value="login">{t("Need a login")}</option>
                  <option value="public">{t("Are public")}</option>
                </select>
              </Field>
              <Field
                label={t("Login path")}
                help={t("The engine serves the login flow under this path.")}
              >
                <input
                  aria-label={t("Login path")}
                  className={MONO}
                  disabled={dis}
                  value={cfg.auth_path}
                  onChange={(e) => set({ auth_path: e.target.value })}
                />
              </Field>
            </div>
            <fieldset className="space-y-2 rounded-lg border p-4">
              <legend className="px-1 text-sm font-medium">
                {t("Headers sent to the origin")}
              </legend>
              <p className="text-xs text-muted-foreground">
                {t(
                  "Any header with these names is removed from the client request first, so the origin can trust them. Leave a name empty to turn it off.",
                )}
              </p>
              <div className="grid gap-3 sm:grid-cols-4">
                {(["user", "email", "groups", "token"] as const).map((k) => (
                  <Field
                    key={k}
                    label={t(
                      k === "token"
                        ? "ID token"
                        : k === "user"
                          ? "User"
                          : k === "email"
                            ? "Email"
                            : "Groups",
                    )}
                  >
                    <input
                      aria-label={`${t("Header for")} ${k}`}
                      className={MONO}
                      disabled={dis}
                      value={cfg.identity_headers[k] ?? ""}
                      onChange={(e) =>
                        set({ identity_headers: { ...cfg.identity_headers, [k]: e.target.value } })
                      }
                    />
                  </Field>
                ))}
              </div>
            </fieldset>
            <section className="space-y-3">
              <h3 className="text-sm font-medium">{t("Rules (the first match decides)")}</h3>
              {cfg.rules.map((r: Obj, i: number) => {
                const upd = (p: Obj) =>
                  rows("rules", (l: Obj[]) => l.map((x, j) => (j === i ? { ...x, ...p } : x)));
                return (
                  <Rule
                    key={i}
                    title={`${t("Rule")} ${i + 1}`}
                    disabled={dis}
                    onRemove={() => rows("rules", (l: Obj[]) => l.filter((_, j) => j !== i))}
                  >
                    <Field label={t("Requires")}>
                      <select
                        aria-label={`Rule ${i + 1} mode`}
                        className={INPUT}
                        disabled={dis}
                        value={r.mode}
                        onChange={(e) => upd({ mode: e.target.value })}
                      >
                        <option value="public">{t("Nothing (public)")}</option>
                        <option value="login">{t("Any signed-in user")}</option>
                        <option value="groups">{t("A member of a group")}</option>
                        <option value="email_domains">{t("An email in a domain")}</option>
                      </select>
                    </Field>
                    {r.mode === "groups" && (
                      <ChipInput
                        label={`Rule ${i + 1} groups`}
                        value={r.groups}
                        onChange={(v) => upd({ groups: v })}
                        freeText={{ validate: () => null }}
                        placeholder={t("Add a group")}
                        disabled={dis}
                      />
                    )}
                    {r.mode === "email_domains" && (
                      <ChipInput
                        label={`Rule ${i + 1} email domains`}
                        value={r.email_domains}
                        onChange={(v) => upd({ email_domains: v })}
                        freeText={{ validate: () => null }}
                        placeholder="example.com"
                        disabled={dis}
                      />
                    )}
                    {r.mode !== "public" && (
                      <PostureEditor
                        id={`Rule ${i + 1}`}
                        posture={r.posture ?? null}
                        disabled={dis}
                        onChange={(p) => upd({ posture: p })}
                      />
                    )}
                    <MatchEditor
                      id={`Rule ${i + 1}`}
                      disabled={dis}
                      match={r.match as Match}
                      onChange={(m) => upd({ match: m })}
                    />
                  </Rule>
                );
              })}
              <Button
                size="sm"
                variant="outline"
                disabled={dis}
                onClick={() =>
                  rows("rules", (l: Obj[]) => [
                    ...l,
                    { match: emptyMatch(), mode: "login", groups: [], email_domains: [] },
                  ])
                }
              >
                {t("Add rule")}
              </Button>
            </section>
          </>
        )}

        {kind === "jwt" && (
          <section className="space-y-3">
            <p className="text-xs text-muted-foreground">
              {t(
                "A request needs a valid bearer token: a signature from one of the keys, the expected issuer and audience, not expired, and every required scope. Anything else is answered 401. Only public keys are stored.",
              )}
            </p>
            {cfg.rules.map((r: Obj, i: number) => {
              const upd = (p: Obj) =>
                rows("rules", (l: Obj[]) => l.map((x, j) => (j === i ? { ...x, ...p } : x)));
              return (
                <Rule
                  key={i}
                  title={`${t("Rule")} ${i + 1}`}
                  disabled={dis}
                  onRemove={() => rows("rules", (l: Obj[]) => l.filter((_, j) => j !== i))}
                >
                  <div className="grid gap-3 sm:grid-cols-3">
                    <Field label={t("Issuer")}>
                      <input
                        aria-label={`Rule ${i + 1} issuer`}
                        className={MONO}
                        disabled={dis}
                        value={r.issuer}
                        onChange={(e) => upd({ issuer: e.target.value })}
                      />
                    </Field>
                    <Field label={t("Leeway (s)")}>
                      <Nums
                        label={`Rule ${i + 1} leeway`}
                        v={r.leeway_s}
                        set={(n) => upd({ leeway_s: n ?? 30 })}
                        min={0}
                        max={300}
                      />
                    </Field>
                    <Field
                      label={t("JWKS URL (optional)")}
                      help={t(
                        "The issuer's key set. Keys arrive within a few minutes; until then the rule answers 401.",
                      )}
                    >
                      <input
                        aria-label={`Rule ${i + 1} JWKS URL`}
                        className={MONO}
                        placeholder="https://idp.example.com/.well-known/jwks.json"
                        disabled={dis}
                        value={r.jwks_url ?? ""}
                        onChange={(e) => upd({ jwks_url: e.target.value })}
                      />
                    </Field>
                    <Field label={t("Subject header (optional)")}>
                      <input
                        aria-label={`Rule ${i + 1} subject header`}
                        className={MONO}
                        disabled={dis}
                        value={r.subject_header ?? ""}
                        onChange={(e) => upd({ subject_header: e.target.value })}
                      />
                    </Field>
                  </div>
                  <div className="space-y-1.5">
                    <div className="text-sm font-medium">{t("Accepted audiences")}</div>
                    <ChipInput
                      label={`Rule ${i + 1} audiences`}
                      value={r.audiences}
                      onChange={(v) => upd({ audiences: v })}
                      freeText={{ validate: () => null }}
                      disabled={dis}
                    />
                  </div>
                  <div className="space-y-1.5">
                    <div className="text-sm font-medium">{t("Required scopes")}</div>
                    <ChipInput
                      label={`Rule ${i + 1} scopes`}
                      value={r.required_scopes}
                      onChange={(v) => upd({ required_scopes: v })}
                      freeText={{ validate: () => null }}
                      disabled={dis}
                    />
                  </div>
                  <div className="space-y-2">
                    <div className="text-sm font-medium">{t("Signing keys (public)")}</div>
                    {r.keys.map((k: Obj, j: number) => (
                      <div key={j} className="space-y-2 rounded-md border p-3">
                        <div className="flex gap-2">
                          <input
                            aria-label={`Rule ${i + 1} key ${j + 1} kid`}
                            placeholder="kid"
                            className={cn(MONO, "w-40")}
                            disabled={dis}
                            value={k.kid ?? ""}
                            onChange={(e) =>
                              upd({
                                keys: r.keys.map((x: Obj, m: number) =>
                                  m === j ? { ...x, kid: e.target.value } : x,
                                ),
                              })
                            }
                          />
                          <select
                            aria-label={`Rule ${i + 1} key ${j + 1} algorithm`}
                            className={cn(INPUT, "w-32")}
                            disabled={dis}
                            value={k.alg}
                            onChange={(e) =>
                              upd({
                                keys: r.keys.map((x: Obj, m: number) =>
                                  m === j ? { ...x, alg: e.target.value } : x,
                                ),
                              })
                            }
                          >
                            {[
                              "RS256",
                              "RS384",
                              "RS512",
                              "ES256",
                              "ES384",
                              "ES512",
                              "PS256",
                              "PS384",
                              "PS512",
                            ].map((a) => (
                              <option key={a}>{a}</option>
                            ))}
                          </select>
                          <Button
                            size="sm"
                            variant="ghost"
                            className="ml-auto"
                            disabled={dis || r.keys.length === 1}
                            aria-label={`${t("Remove key")} ${j + 1}`}
                            onClick={() =>
                              upd({ keys: r.keys.filter((_: Obj, m: number) => m !== j) })
                            }
                          >
                            <X className="size-4" />
                          </Button>
                        </div>
                        <textarea
                          aria-label={`Rule ${i + 1} key ${j + 1} public key`}
                          placeholder={"-----BEGIN PUBLIC KEY-----"}
                          className={cn(MONO, "h-24 py-2 text-xs")}
                          disabled={dis}
                          value={k.public_key_pem}
                          onChange={(e) =>
                            upd({
                              keys: r.keys.map((x: Obj, m: number) =>
                                m === j ? { ...x, public_key_pem: e.target.value } : x,
                              ),
                            })
                          }
                        />
                      </div>
                    ))}
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={dis}
                      onClick={() =>
                        upd({ keys: [...r.keys, { kid: "", alg: "RS256", public_key_pem: "" }] })
                      }
                    >
                      {t("Add key")}
                    </Button>
                    <JwksImport
                      id={`Rule ${i + 1}`}
                      disabled={dis}
                      keys={r.keys}
                      onKeys={(k) => upd({ keys: k })}
                    />
                  </div>
                  <MatchEditor
                    id={`Rule ${i + 1}`}
                    disabled={dis}
                    match={r.match as Match}
                    onChange={(m) => upd({ match: m })}
                  />
                </Rule>
              );
            })}
            <Button
              size="sm"
              variant="outline"
              disabled={dis}
              onClick={() => rows("rules", (l: Obj[]) => [...l, defaults("jwt").rules[0]])}
            >
              {t("Add rule")}
            </Button>
          </section>
        )}

        {kind === "client_cert" && (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label={t("User header")}>
                <input
                  aria-label={t("User header")}
                  className={MONO}
                  disabled={dis}
                  value={cfg.user_header ?? ""}
                  onChange={(e) => set({ user_header: e.target.value })}
                />
              </Field>
              <Field label={t("Groups header")}>
                <input
                  aria-label={t("Groups header")}
                  className={MONO}
                  disabled={dis}
                  value={cfg.groups_header ?? ""}
                  onChange={(e) => set({ groups_header: e.target.value })}
                />
              </Field>
            </div>
            <section className="space-y-3">
              <h3 className="text-sm font-medium">{t("Known certificates")}</h3>
              {cfg.identities.map((c: Obj, i: number) => {
                const upd = (p: Obj) =>
                  rows("identities", (l: Obj[]) => l.map((x, j) => (j === i ? { ...x, ...p } : x)));
                return (
                  <div key={i} className="grid grid-cols-[7rem_1fr_1fr_1fr_auto] items-end gap-2">
                    <select
                      aria-label={`Identity ${i + 1} match by`}
                      className={INPUT}
                      disabled={dis}
                      value={c.by}
                      onChange={(e) => upd({ by: e.target.value })}
                    >
                      <option value="cn">{t("Common name")}</option>
                      <option value="sha1">SHA-1</option>
                    </select>
                    <input
                      aria-label={`Identity ${i + 1} value`}
                      className={MONO}
                      disabled={dis}
                      value={c.value}
                      onChange={(e) => upd({ value: e.target.value })}
                    />
                    <input
                      aria-label={`Identity ${i + 1} user`}
                      placeholder={t("User")}
                      className={INPUT}
                      disabled={dis}
                      value={c.user}
                      onChange={(e) => upd({ user: e.target.value })}
                    />
                    <ChipInput
                      label={`Identity ${i + 1} groups`}
                      value={c.groups ?? []}
                      onChange={(v) => upd({ groups: v })}
                      freeText={{ validate: () => null }}
                      placeholder={t("Groups")}
                      disabled={dis}
                    />
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={dis}
                      aria-label={`${t("Remove identity")} ${i + 1}`}
                      onClick={() => rows("identities", (l: Obj[]) => l.filter((_, j) => j !== i))}
                    >
                      <X className="size-4" />
                    </Button>
                  </div>
                );
              })}
              <Button
                size="sm"
                variant="outline"
                disabled={dis}
                onClick={() =>
                  rows("identities", (l: Obj[]) => [
                    ...l,
                    { by: "cn", value: "", user: "", groups: [] },
                  ])
                }
              >
                {t("Add certificate")}
              </Button>
              <CertCsvImport
                disabled={dis}
                identities={cfg.identities}
                onIdentities={(l) => set({ identities: l })}
              />
            </section>
            <section className="space-y-3">
              <h3 className="text-sm font-medium">
                {t("Rules (the first match decides; requests that match none are not checked)")}
              </h3>
              {cfg.rules.map((r: Obj, i: number) => {
                const upd = (p: Obj) =>
                  rows("rules", (l: Obj[]) => l.map((x, j) => (j === i ? { ...x, ...p } : x)));
                return (
                  <Rule
                    key={i}
                    title={`${t("Rule")} ${i + 1}`}
                    disabled={dis}
                    onRemove={() => rows("rules", (l: Obj[]) => l.filter((_, j) => j !== i))}
                  >
                    <Field label={t("Requires")}>
                      <select
                        aria-label={`Rule ${i + 1} mode`}
                        className={INPUT}
                        disabled={dis}
                        value={r.mode}
                        onChange={(e) => upd({ mode: e.target.value })}
                      >
                        <option value="valid">
                          {t("Any certificate the port's client CA accepts")}
                        </option>
                        <option value="identity">{t("A known certificate")}</option>
                        <option value="groups">{t("A known certificate in a group")}</option>
                      </select>
                    </Field>
                    {r.mode === "groups" && (
                      <ChipInput
                        label={`Rule ${i + 1} groups`}
                        value={r.groups}
                        onChange={(v) => upd({ groups: v })}
                        freeText={{ validate: () => null }}
                        placeholder={t("Add a group")}
                        disabled={dis}
                      />
                    )}
                    <MatchEditor
                      id={`Rule ${i + 1}`}
                      disabled={dis}
                      match={r.match as Match}
                      onChange={(m) => upd({ match: m })}
                    />
                  </Rule>
                );
              })}
              <Button
                size="sm"
                variant="outline"
                disabled={dis}
                onClick={() =>
                  rows("rules", (l: Obj[]) => [
                    ...l,
                    { match: emptyMatch(), mode: "valid", groups: [] },
                  ])
                }
              >
                {t("Add rule")}
              </Button>
            </section>
          </>
        )}

        {kind === "waf" && (
          <div className="space-y-4">
            <p className="rounded-md bg-muted p-3 text-xs">
              {t(
                "New policies start in Detect: requests are inspected and logged, nothing is blocked, so you can read the logs before turning blocking on. A service can carry one WAF policy.",
              )}
            </p>
            <div className="grid gap-3 sm:grid-cols-3">
              <fieldset className="space-y-1.5 sm:col-span-3">
                <legend className="text-sm font-medium">{t("Mode")}</legend>
                <div className="grid gap-2 sm:grid-cols-2">
                  {(
                    [
                      [
                        "detect",
                        "Detect",
                        "Log what would be blocked; traffic is not touched. Start here.",
                      ],
                      ["block", "Block", "Refuse requests that score over the threshold."],
                    ] as const
                  ).map(([k, label, help]) => (
                    <label
                      key={k}
                      className={cn(
                        "flex cursor-pointer items-start gap-2 rounded-lg border bg-card p-2.5 text-sm",
                        cfg.mode === k && "border-primary bg-primary/10",
                      )}
                    >
                      <input
                        type="radio"
                        name="waf-mode"
                        aria-label={t(label)}
                        className="mt-1"
                        disabled={dis}
                        checked={cfg.mode === k}
                        onChange={() => set({ mode: k })}
                      />
                      <span>
                        <span className="font-medium">{t(label)}</span>
                        <span className="block text-xs text-muted-foreground">{t(help)}</span>
                      </span>
                    </label>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  {t("Detected requests show in")}{" "}
                  <Link
                    to="/t/$tenant/logs"
                    params={{ tenant }}
                    className="text-primary hover:underline"
                  >
                    {t("Request logs")}
                  </Link>
                  .
                </p>
              </fieldset>
              <Field
                label={t("If the WAF fails")}
                help={t("Open lets traffic through; closed answers an error.")}
              >
                <select
                  aria-label={t("Fail mode")}
                  className={INPUT}
                  disabled={dis}
                  value={cfg.fail_mode}
                  onChange={(e) => set({ fail_mode: e.target.value })}
                >
                  <option value="open">{t("Fail open")}</option>
                  <option value="closed">{t("Fail closed")}</option>
                </select>
              </Field>
              <Field label={t("Rule set")}>
                <input
                  aria-label={t("Core Rule Set version")}
                  className={MONO}
                  disabled
                  value={`OWASP CRS ${cfg.crs_version}`}
                  readOnly
                />
              </Field>
              <Field
                label={t("Paranoia level")}
                help={t("1 is the fewest rules and false positives; 4 the most.")}
              >
                <select
                  aria-label={t("Paranoia level")}
                  className={INPUT}
                  disabled={dis}
                  value={cfg.paranoia_level}
                  onChange={(e) => set({ paranoia_level: Number(e.target.value) })}
                >
                  {[1, 2, 3, 4].map((n) => (
                    <option key={n} value={n}>
                      {n}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t("Inbound threshold")}>
                <Nums
                  label={t("Inbound threshold")}
                  v={cfg.inbound_threshold}
                  set={(n) => set({ inbound_threshold: n ?? 5 })}
                  min={1}
                />
              </Field>
              <Field label={t("Outbound threshold")}>
                <Nums
                  label={t("Outbound threshold")}
                  v={cfg.outbound_threshold}
                  set={(n) => set({ outbound_threshold: n ?? 4 })}
                  min={1}
                />
              </Field>
              <Field label={t("Time limit (ms)")}>
                <Nums
                  label={t("WAF time limit")}
                  v={cfg.timeout_ms}
                  set={(n) => set({ timeout_ms: n ?? 100 })}
                  min={1}
                />
              </Field>
              <label className="flex items-center gap-2 pt-6 text-sm">
                <Switch
                  aria-label={t("Inspect request bodies")}
                  checked={cfg.inspect_body}
                  disabled={dis}
                  onCheckedChange={(on) => set({ inspect_body: on })}
                />
                {t("Inspect request bodies")}
              </label>
            </div>
            <div className="space-y-1.5">
              <div className="text-sm font-medium">{t("Disabled rule IDs")}</div>
              <ChipInput
                label={t("Disabled rule IDs")}
                options={crsOptions}
                value={(cfg.disabled_rule_ids as number[]).map(String)}
                onChange={(v) => set({ disabled_rule_ids: v.map(Number) })}
                freeText={{
                  validate: (x) =>
                    /^\d{3,7}$/.test(x.trim()) ? null : t("Use a rule number such as 942100."),
                }}
                placeholder="942100"
                disabled={dis}
              />
              <p className="text-xs text-muted-foreground">
                {t("Switch off one rule that blocks legitimate traffic.")}{" "}
                <a
                  href="https://coreruleset.org/docs/"
                  target="_blank"
                  rel="noreferrer"
                  className="text-primary hover:underline"
                  aria-label={t("OWASP Core Rule Set documentation")}
                >
                  ?
                </a>
              </p>
            </div>
            <div className="space-y-1.5">
              <div className="text-sm font-medium">{t("Paths not inspected")}</div>
              <ChipInput
                label={t("Paths not inspected")}
                value={cfg.excluded_paths}
                onChange={(v) => set({ excluded_paths: v })}
                freeText={{
                  validate: (x) => (x.trim().startsWith("/") ? null : t("A path starts with /.")),
                }}
                placeholder="/health"
                disabled={dis}
              />
            </div>
          </div>
        )}

        {kind === "protection" && (
          <div className="space-y-4">
            <p className="text-xs text-muted-foreground">
              {t(
                "Per-source connection limits, slow-request timeouts and user-agent rules. Request-rate limits are Rate limit policies.",
              )}
            </p>
            <div className="grid gap-3 sm:grid-cols-3">
              <Field
                label={t("Open connections per source")}
                help={t("More are refused. Empty is no limit.")}
              >
                <Nums
                  label={t("Connections per source")}
                  v={cfg.max_connections_per_source}
                  set={(n) => set({ max_connections_per_source: n })}
                  min={1}
                />
                {cfg.max_connections_per_source != null && cfg.max_connections_per_source < 50 && (
                  <span role="note" className="block text-xs text-status-degraded">
                    {t(
                      "Below 50, people behind one shared address (an office, a mobile carrier) will be refused.",
                    )}
                  </span>
                )}
              </Field>
              <Field label={t("New connections per source")}>
                <Nums
                  label={t("New connections per source")}
                  v={cfg.max_new_connections_per_source}
                  set={(n) => set({ max_new_connections_per_source: n })}
                  min={1}
                />
              </Field>
              <Field label={t("Per")}>
                <select
                  aria-label={t("Connection rate period")}
                  className={INPUT}
                  disabled={dis}
                  value={cfg.connection_rate_period_s}
                  onChange={(e) => set({ connection_rate_period_s: Number(e.target.value) })}
                >
                  {[1, 10, 60].map((p) => (
                    <option key={p} value={p}>
                      {p === 60 ? t("minute") : t("{n} s", { n: p })}
                    </option>
                  ))}
                </select>
              </Field>
              <Field
                label={t("Time to send the request head (s)")}
                help={t("The answer to slow-header attacks.")}
              >
                <Nums
                  label={t("Header timeout")}
                  v={cfg.header_timeout_s}
                  set={(n) => set({ header_timeout_s: n })}
                  min={1}
                />
              </Field>
              <Field label={t("Idle client connection (s)")}>
                <Nums
                  label={t("Client timeout")}
                  v={cfg.client_timeout_s}
                  set={(n) => set({ client_timeout_s: n })}
                  min={1}
                />
              </Field>
              <Field label={t("Tracked sources")}>
                <Nums
                  label={t("Tracked sources")}
                  v={cfg.table_size}
                  set={(n) => set({ table_size: n ?? 100000 })}
                  min={1}
                />
              </Field>
            </div>
            <div className="flex flex-wrap gap-6 text-sm">
              <label className="flex items-center gap-2">
                <Switch
                  aria-label={t("Refuse an empty User-Agent")}
                  checked={cfg.block_empty_user_agent}
                  disabled={dis}
                  onCheckedChange={(on) => set({ block_empty_user_agent: on })}
                />
                {t("Refuse an empty User-Agent")}
              </label>
              <label className="flex items-center gap-2">
                <Switch
                  aria-label={t("Refuse known scanners")}
                  checked={cfg.block_scanners}
                  disabled={dis}
                  onCheckedChange={(on) => set({ block_scanners: on })}
                />
                {t("Refuse known scanners")}
              </label>
            </div>
            <div className="space-y-1.5">
              <div className="text-sm font-medium">{t("More User-Agent text to refuse")}</div>
              <ChipInput
                label={t("User-Agent text to refuse")}
                value={cfg.deny_user_agents}
                onChange={(v) => set({ deny_user_agents: v.map((x) => x.toLowerCase()) })}
                freeText={{ validate: () => null }}
                placeholder="sqlmap"
                disabled={dis}
              />
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label={t("Bots are")}>
                <select
                  aria-label={t("Bot action")}
                  className={INPUT}
                  disabled={dis}
                  value={cfg.bot_action}
                  onChange={(e) => set({ bot_action: e.target.value })}
                >
                  <option value="deny">{t("Answered 403")}</option>
                  <option value="silent_drop">{t("Dropped silently")}</option>
                </select>
              </Field>
              <Field
                label={t("Sources exempt from every rule")}
                help={t("One IPv4 or IPv6 network per line.")}
              >
                <textarea
                  aria-label={t("Exempt networks")}
                  className={cn(MONO, "h-20 py-2 text-xs")}
                  disabled={dis}
                  defaultValue={exempt}
                  onChange={(e) =>
                    set({
                      exempt_cidrs: e.target.value
                        .split("\n")
                        .map((x) => x.trim())
                        .filter(Boolean),
                    })
                  }
                />
                {exemptBad && (
                  <span role="alert" className="block text-xs text-status-down">
                    {t("Not a valid address or network")}
                  </span>
                )}
              </Field>
            </div>
            <NewVisitorCheck cfg={cfg} set={set} disabled={dis} />
          </div>
        )}
      </div>
      <div className="flex items-center gap-2 border-t px-5 py-3">
        {policy && (
          <Button
            variant="ghost"
            disabled={dis || usedBy.length > 0}
            onClick={() => setConfirmDelete(true)}
          >
            {t("Delete")}
          </Button>
        )}
        <Button variant="ghost" className="ml-auto" onClick={onClose}>
          {t("Cancel")}
        </Button>
        <Button disabled={dis || !name || save.isPending} onClick={() => void submit()}>
          {t("Save to draft")}
        </Button>
      </div>
      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={t("Delete {name}?", { name: policy?.name ?? "" })}
        confirmLabel={t("Delete")}
        onConfirm={async () => {
          if (!policy) return;
          try {
            await save.mutateAsync({ action: "delete", id: policy.id, version: policy.version });
            onClose();
          } catch (e) {
            setProblem(e);
          } finally {
            setConfirmDelete(false);
          }
        }}
      >
        <p>
          {t(
            "The policy is removed in your draft. Nothing goes live until the change set is applied.",
          )}
        </p>
      </ConfirmDialog>
    </section>
  );
}
