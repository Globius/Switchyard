import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { AlertTriangle, Clock, XCircle } from "lucide-react";
import { useMemo, useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { ChipInput, validateHostname } from "@/components/sy/ChipInput";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { DraftMarker, expiryStatus, ExpiryPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { Tip } from "@/components/ui/tooltip";
import { useSaveOp } from "@/features/objects/save";
import { useDraft } from "@/features/shell/draft";
import { useEnsureDraft } from "@/features/shell/draft-write";
import { useCertificates } from "@/features/vs/queries";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";
import { shortDate } from "@/lib/format";

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";
const SOURCE: Record<string, string> = {
  upload: "Upload",
  cloudflare_origin: "Cloudflare Origin CA",
  acme: "ACME",
  step_ca: "Step CA",
};

/** True when the text holds a certificate block and a private key block (both PEM). */
export function looksLikePem(pem: string): { cert: boolean; key: boolean } {
  return {
    cert: /-----BEGIN CERTIFICATE-----[\s\S]+-----END CERTIFICATE-----/.test(pem),
    key: /-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----[\s\S]+-----END (?:[A-Z]+ )?PRIVATE KEY-----/.test(
      pem,
    ),
  };
}

function useServices(tenant: string, cs?: string) {
  return useQuery({
    queryKey: ["t", tenant, "virtual-services", "all", { cs }],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/virtual-services", {
          params: { path: { t: tenant }, query: { limit: 200, change_set: cs } },
        }),
      ),
  });
}

/** A PEM field with its own file picker; the text never leaves the browser until Add to draft. */
function PemField({
  label,
  hint,
  value,
  onChange,
  ok,
}: {
  label: string;
  hint?: string;
  value: string;
  onChange: (v: string) => void;
  ok?: boolean;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2 text-sm font-medium">
        <label htmlFor={`pem-${label}`}>{label}</label>
        {value.trim() && <span aria-hidden>{ok ? "✓" : "○"}</span>}
        <label className="ml-auto cursor-pointer text-xs font-normal text-primary hover:underline">
          {t("Choose file")}
          <input
            type="file"
            aria-label={t("{label} file", { label })}
            accept=".pem,.crt,.cer,.key,text/plain"
            className="sr-only"
            onChange={async (e) => {
              const f = e.target.files?.[0];
              if (f) onChange(await f.text());
              e.target.value = "";
            }}
          />
        </label>
      </div>
      <textarea
        id={`pem-${label}`}
        aria-label={label}
        className="h-28 w-full rounded-md border bg-card p-3 font-mono text-xs"
        spellCheck={false}
        autoComplete="off"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

/** Join the three fields into the single PEM bundle the API takes: leaf, chain, then the key. */
export function buildBundle(cert: string, chain: string, key: string): string {
  return (
    [cert, chain, key]
      .map((x) => x.trim())
      .filter(Boolean)
      .join("\n") + "\n"
  );
}

/** Base64 of raw bytes (a PKCS#12 file), without a big spread that overflows on large files. */
export function toBase64(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000)
    bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

/** Import a new certificate, or with `replace` swap the material of an existing one (same id and usages). */
/** Import a CA certificate (and optional CRL) that virtual services use to verify client certificates. */
function ImportCaSheet({ tenant, onClose }: { tenant: string; onClose: () => void }) {
  const ensure = useEnsureDraft(tenant);
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [pem, setPem] = useState("");
  const [crl, setCrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);
  const pemOk = looksLikePem(pem).cert;
  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={t("Import client CA")}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            disabled={busy || !name || !pemOk}
            onClick={async () => {
              setBusy(true);
              setProblem(null);
              try {
                const cs = await ensure.mutateAsync();
                unwrap(
                  await api.POST("/api/v1/tenants/{t}/certificates/import-ca", {
                    params: { path: { t: tenant }, query: { change_set: cs } },
                    body: { name, pem, ...(crl.trim() ? { crl_pem: crl } : {}) },
                  }),
                );
                await qc.invalidateQueries({ queryKey: ["t", tenant] });
                onClose();
              } catch (e) {
                setProblem(e);
              } finally {
                setBusy(false);
              }
            }}
          >
            {t("Add to draft")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      <p className="text-sm text-muted-foreground">
        {t(
          "A CA certificate for verifying client certificates on TLS ports. It has no private key. Add its revocation list (CRL) to refuse revoked clients.",
        )}
      </p>
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Name")}
        <input
          aria-label={t("Name")}
          className={INPUT}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <PemField
        label={t("CA certificate")}
        value={pem}
        onChange={setPem}
        ok={pemOk}
        hint={t("PEM. One or more CA certificates.")}
      />
      <PemField
        label={t("CRL (optional)")}
        value={crl}
        onChange={setCrl}
        ok={!crl.trim() || crl.includes("BEGIN X509 CRL")}
        hint={t("PEM revocation list.")}
      />
    </Sheet>
  );
}

function ImportSheet({
  tenant,
  onClose,
  replace,
}: {
  tenant: string;
  onClose: () => void;
  replace?: { id: string; name: string };
}) {
  const ensure = useEnsureDraft(tenant);
  const qc = useQueryClient();
  const [name, setName] = useState(replace?.name ?? "");
  const [cert, setCert] = useState("");
  const [chain, setChain] = useState("");
  const [key, setKey] = useState("");
  const [mode, setMode] = useState<"pem" | "pfx">("pem");
  const [pfx, setPfx] = useState<{ name: string; b64: string } | null>(null);
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);
  const certOk = looksLikePem(cert).cert;
  const chainOk = !chain.trim() || looksLikePem(chain).cert;
  const keyOk = looksLikePem(key).key;
  async function submit() {
    setBusy(true);
    setProblem(null);
    try {
      const cs = await ensure.mutateAsync();
      const material =
        mode === "pem"
          ? { pem: buildBundle(cert, chain, key) }
          : { pkcs12_b64: pfx!.b64, password };
      if (replace)
        unwrap(
          await api.POST("/api/v1/tenants/{t}/certificates/{certificate}/replace", {
            params: { path: { t: tenant, certificate: replace.id }, query: { change_set: cs } },
            body: material,
          }),
        );
      else
        unwrap(
          await api.POST("/api/v1/tenants/{t}/certificates/import", {
            params: { path: { t: tenant }, query: { change_set: cs } },
            body: { name, ...material },
          }),
        );
      setKey("");
      setPassword("");
      await qc.invalidateQueries({ queryKey: ["t", tenant] });
      onClose();
    } catch (e) {
      setProblem(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={replace ? t("Replace {name}", { name: replace.name }) : t("Import certificate")}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            disabled={busy || !name || (mode === "pem" ? !certOk || !chainOk || !keyOk : !pfx)}
            onClick={() => void submit()}
          >
            {replace ? t("Replace in draft") : t("Add to draft")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      {replace ? (
        <p className="text-sm text-muted-foreground">
          {t(
            "The new certificate and key take over {name}. Its id and the virtual services using it stay; the swap goes into your draft.",
            { name: replace.name },
          )}
        </p>
      ) : (
        <label className="block space-y-1.5 text-sm font-medium">
          {t("Name")}
          <input
            aria-label={t("Name")}
            className={INPUT}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
      )}
      <div
        role="group"
        aria-label={t("Import format")}
        className="inline-flex overflow-hidden rounded-md border text-sm"
      >
        {(["pem", "pfx"] as const).map((m) => (
          <button
            key={m}
            type="button"
            aria-pressed={mode === m}
            onClick={() => setMode(m)}
            className="px-3 py-1.5 aria-pressed:bg-accent"
          >
            {m === "pem" ? t("PEM files") : t("PKCS#12 / PFX")}
          </button>
        ))}
      </div>
      {mode === "pem" ? (
        <>
          <PemField
            label={t("Certificate")}
            value={cert}
            onChange={setCert}
            ok={certOk}
            hint={t("The leaf certificate (PEM).")}
          />
          <PemField
            label={t("Chain (optional)")}
            value={chain}
            onChange={setChain}
            ok={chainOk}
            hint={t("Intermediate certificates, in order.")}
          />
          <PemField
            label={t("Private key")}
            value={key}
            onChange={setKey}
            ok={keyOk}
            hint={t("Unencrypted PEM. Sent once, stored in OpenBao, never shown again.")}
          />
        </>
      ) : (
        <div className="space-y-3">
          <label className="block space-y-1.5 text-sm font-medium">
            {t("PKCS#12 file (.p12 or .pfx)")}
            <input
              type="file"
              aria-label={t("PKCS#12 file")}
              accept=".p12,.pfx,application/x-pkcs12"
              className="block w-full text-sm"
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (!f) return setPfx(null);
                setPfx({ name: f.name, b64: toBase64(new Uint8Array(await f.arrayBuffer())) });
              }}
            />
          </label>
          {pfx && (
            <p className="text-xs text-muted-foreground">
              {t("Selected {file}", { file: pfx.name })}
            </p>
          )}
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Password")}
            <input
              aria-label={t("Password")}
              type="password"
              autoComplete="off"
              className={INPUT}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
            <span className="block text-xs font-normal text-muted-foreground">
              {t(
                "Leave empty if the file has no password. The file and password are sent once, never stored or shown again; the key goes to OpenBao.",
              )}
            </span>
          </label>
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        {t(
          "SANs and expiry are read from the certificate. Importing adds the certificate to your draft.",
        )}
      </p>
    </Sheet>
  );
}

type CertRow = components["schemas"]["CertificateRead"];
type ServiceRow = components["schemas"]["VirtualServiceRead"];

/** A certificate's details and every virtual service that uses it (live or in your draft). */
/** Table and detail wording for the renewal setting: `Auto · 30 days before` or `Manual`. */
export function renewalLabel(c: { renew_policy: string; renew_before_days?: number }): string {
  return c.renew_policy === "auto"
    ? t("Auto · {n} days before", { n: c.renew_before_days ?? 30 })
    : t("Manual");
}

function RenewalEditor({ tenant, cert }: { tenant: string; cert: CertRow }) {
  const save = useSaveOp(tenant, "certificate");
  const can = useCan(tenant);
  const write = can("write", "application");
  const [policy, setPolicy] = useState<"auto" | "manual">(cert.renew_policy);
  const [days, setDays] = useState(String(cert.renew_before_days ?? 30));
  const [problem, setProblem] = useState<unknown>(null);
  const n = Number(days);
  const valid = Number.isInteger(n) && n >= 1 && n <= 365;
  const dirty = policy !== cert.renew_policy || n !== (cert.renew_before_days ?? 30);
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold">{t("Renewal")}</h3>
      {problem != null && <ErrorState error={problem} />}
      <div className="flex flex-wrap items-end gap-3 text-sm">
        <label className="space-y-1">
          <span className="block font-medium">{t("Renew")}</span>
          <select
            aria-label={t("Renew")}
            className={INPUT}
            disabled={!write.allowed}
            value={policy}
            onChange={(e) => setPolicy(e.target.value as "auto" | "manual")}
          >
            <option value="auto">{t("Automatically")}</option>
            <option value="manual">{t("Manually")}</option>
          </select>
        </label>
        <label className="space-y-1">
          <span className="block font-medium">{t("Days before expiry")}</span>
          <input
            aria-label={t("Days before expiry")}
            type="number"
            min={1}
            max={365}
            className={cn(INPUT, "w-28")}
            disabled={!write.allowed || policy !== "auto"}
            value={days}
            onChange={(e) => setDays(e.target.value)}
          />
        </label>
        <Tip label={write.reason}>
          <Button
            variant="outline"
            disabled={!write.allowed || !dirty || !valid || save.isPending}
            onClick={async () => {
              setProblem(null);
              try {
                await save.mutateAsync({
                  action: "update",
                  id: cert.id,
                  version: cert.version,
                  body: { renew_policy: policy, renew_before_days: n },
                });
              } catch (e) {
                setProblem(e);
              }
            }}
          >
            {t("Save to draft")}
          </Button>
        </Tip>
      </div>
      {!valid && (
        <p role="alert" className="text-xs text-status-down">
          {t("Enter a whole number of days from 1 to 365.")}
        </p>
      )}
    </section>
  );
}

/** OCSP stapling: engines attach the CA's revocation answer. Not offered for Cloudflare Origin CA certificates. */
function OcspEditor({ tenant, cert }: { tenant: string; cert: CertRow }) {
  const save = useSaveOp(tenant, "certificate");
  const can = useCan(tenant);
  const write = can("write", "application");
  const [problem, setProblem] = useState<unknown>(null);
  if (cert.source === "cloudflare_origin") return null;
  return (
    <section className="space-y-2">
      <div className="flex items-center gap-3">
        <h3 className="text-sm font-semibold">{t("OCSP stapling")}</h3>
        <Tip label={write.reason}>
          <span>
            <Switch
              aria-label={t("OCSP stapling")}
              checked={cert.ocsp_stapling}
              disabled={!write.allowed || save.isPending}
              onCheckedChange={async (on) => {
                setProblem(null);
                try {
                  await save.mutateAsync({
                    action: "update",
                    id: cert.id,
                    version: cert.version,
                    body: { ocsp_stapling: on },
                  });
                } catch (e) {
                  setProblem(e);
                }
              }}
            />
          </span>
        </Tip>
      </div>
      <p className="text-xs text-muted-foreground">
        {t("Engines fetch the CA's revocation answer and send it with the certificate.")}
      </p>
      {problem != null && <ErrorState error={problem} />}
    </section>
  );
}

function UsageSheet({
  tenant,
  cert,
  services,
  onClose,
  onReplace,
}: {
  tenant: string;
  cert: CertRow | undefined;
  services: ServiceRow[];
  onClose: () => void;
  onReplace?: () => void;
}) {
  if (!cert) return null;
  const ex = expiryStatus(cert.not_after);
  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={cert.name}
      badge={
        cert.draft_status !== "unchanged" ? <DraftMarker status={cert.draft_status} /> : undefined
      }
      footer={
        <>
          {onReplace && cert.source === "upload" && (
            <Button variant="outline" onClick={onReplace}>
              {t("Replace")}
            </Button>
          )}
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Close")}
          </Button>
        </>
      }
    >
      <dl className="grid grid-cols-[7rem_1fr] gap-y-2 text-sm">
        <dt className="text-muted-foreground">{t("Source")}</dt>
        <dd>{t(SOURCE[cert.source] ?? cert.source)}</dd>
        <dt className="text-muted-foreground">{t("Names")}</dt>
        <dd className="font-mono text-xs">{cert.sans.join(", ")}</dd>
        <dt className="text-muted-foreground">{t("Expires")}</dt>
        <dd className="flex items-center gap-2">
          <ExpiryPill ex={ex} />
          {shortDate(cert.not_after)}
        </dd>
        <dt className="text-muted-foreground">{t("Renewal")}</dt>
        <dd>{renewalLabel(cert)}</dd>
        <dt className="text-muted-foreground">{t("Key")}</dt>
        <dd className="font-mono text-xs">{cert.secret_ref}</dd>
      </dl>
      <RenewalEditor key={`${cert.id}-${cert.version}`} tenant={tenant} cert={cert} />
      <OcspEditor key={`o-${cert.id}-${cert.version}`} tenant={tenant} cert={cert} />
      <section className="space-y-2">
        <h3 className="text-sm font-semibold">
          {t("Used by {n} virtual services", { n: services.length })}
        </h3>
        {services.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {t("No virtual service uses this certificate, so it can be deleted.")}
          </p>
        ) : (
          <ul className="divide-y rounded-lg border">
            {services.map((v) => (
              <li key={v.id} className="flex items-center gap-3 px-3 py-2.5 text-sm">
                <Link
                  to="/t/$tenant/virtual-services/$id"
                  params={{ tenant, id: v.id }}
                  className="font-medium text-primary hover:underline"
                >
                  {v.name}
                </Link>
                {v.draft_status !== "unchanged" && <DraftMarker status={v.draft_status} />}
                <span className="ml-auto font-mono text-xs text-muted-foreground">
                  {v.vip ?? "—"} ·{" "}
                  {v.ports
                    .filter((p) => p.tls)
                    .map((p) => p.port)
                    .join(", ") || "—"}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </Sheet>
  );
}

function IssueSheet({ tenant, onClose }: { tenant: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [source, setSource] = useState<"acme" | "cloudflare_origin">("acme");
  const [name, setName] = useState("");
  const [sans, setSans] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);
  async function submit() {
    setBusy(true);
    setProblem(null);
    try {
      unwrap(
        await api.POST("/api/v1/tenants/{t}/certificates/issue", {
          params: { path: { t: tenant } },
          body: { name, source, sans },
        }),
      );
      await qc.invalidateQueries({ queryKey: ["t", tenant] });
      setDone(true);
    } catch (e) {
      setProblem(e);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={t("Issue certificate")}
      centered
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {done ? t("Close") : t("Cancel")}
          </Button>
          {!done && (
            <Button disabled={busy || !name || sans.length === 0} onClick={() => void submit()}>
              {t("Start issuing")}
            </Button>
          )}
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      {done ? (
        <p role="status" className="text-sm">
          {t(
            "Issuing has started as a job. When it finishes it opens a system:certs change set that follows approval like any other.",
          )}
        </p>
      ) : (
        <>
          <fieldset className="space-y-2">
            <legend className="text-sm font-medium">{t("Issuer")}</legend>
            <div className="grid grid-cols-2 gap-3">
              {(
                [
                  ["acme", "ACME", "Publicly trusted. DNS-01 through Cloudflare."],
                  [
                    "cloudflare_origin",
                    "Cloudflare Origin CA",
                    "Trusted by Cloudflare only, for proxied hosts.",
                  ],
                ] as const
              ).map(([k, label, help]) => (
                <label
                  key={k}
                  className={cn(
                    "flex cursor-pointer gap-3 rounded-lg border p-3",
                    source === k && "border-primary bg-primary/10",
                  )}
                >
                  <input
                    type="radio"
                    name="issuer"
                    aria-label={label}
                    checked={source === k}
                    onChange={() => setSource(k)}
                  />
                  <span>
                    <span className="font-medium">{label}</span>
                    <span className="block text-sm text-muted-foreground">{t(help)}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Name")}
            <input
              aria-label={t("Name")}
              className={INPUT}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <div className="space-y-1.5">
            <div className="text-sm font-medium">{t("Names (SANs)")}</div>
            <ChipInput
              label={t("Names (SANs)")}
              value={sans}
              onChange={setSans}
              freeText={{ validate: validateHostname }}
              placeholder="docs.example.com"
            />
            <span className="block text-xs text-muted-foreground">
              {t(
                "Press Enter or comma to add a name. Each must be in a Cloudflare zone this tenant can edit.",
              )}
            </span>
          </div>
          <p className="rounded-md bg-muted p-3 text-sm">
            {t(
              "Runs as a job. The key is generated on the controller and stored in OpenBao. When the certificate is ready, a change set adds it; it waits for approval like any other.",
            )}
          </p>
        </>
      )}
    </Sheet>
  );
}

/** Certificates: source, SANs, expiry, renewal and which services use each. Keys live only in OpenBao. */
export function Certificates({ tenant }: { tenant: string }) {
  const { draft } = useDraft(tenant);
  const list = useCertificates(tenant, draft?.id);
  const services = useServices(tenant, draft?.id);
  const can = useCan(tenant);
  const write = can("write", "application");
  const runtime = can("manage", "certificate");
  const del = useSaveOp(tenant, "certificate");
  const qc = useQueryClient();
  const [sheet, setSheet] = useState<"import" | "import-ca" | "issue" | null>(null);
  const [replacing, setReplacing] = useState<{ id: string; name: string } | null>(null);
  const [detail, setDetail] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<{ id: string; name: string; version: number } | null>(
    null,
  );
  const [problem, setProblem] = useState<unknown>(null);

  const usedBy = useMemo(() => {
    const m = new Map<string, string[]>();
    for (const v of services.data?.items ?? [])
      for (const c of v.certificate_ids) m.set(c, [...(m.get(c) ?? []), v.name]);
    return m;
  }, [services.data]);
  const [q, setQ] = useState("");
  const [expiryFilter, setExpiryFilter] = useState<"" | "30" | "14" | "7">("");
  const [issuer, setIssuer] = useState("");
  const all = [...(list.data?.items ?? [])].sort((a, b) => a.not_after.localeCompare(b.not_after));
  const rows = all.filter((c) => {
    if (issuer && c.source !== issuer) return false;
    if (expiryFilter && expiryStatus(c.not_after).days > Number(expiryFilter)) return false;
    const needle = q.trim().toLowerCase();
    return !needle || `${c.name} ${c.sans.join(" ")}`.toLowerCase().includes(needle);
  });

  async function renew(id: string) {
    setProblem(null);
    try {
      unwrap(
        await api.POST("/api/v1/tenants/{t}/certificates/{certificate}/renew", {
          params: { path: { t: tenant, certificate: id } },
        }),
      );
      await qc.invalidateQueries({ queryKey: ["t", tenant] });
    } catch (e) {
      setProblem(e);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t("Certificates")}</h1>
          <p className="text-sm text-muted-foreground">
            {t("{n} certificates · sorted by expiry · keys live only in OpenBao", {
              n: rows.length,
            })}
          </p>
        </div>
        <div className="flex gap-2">
          <Tip label={runtime.reason}>
            <Button
              variant="outline"
              disabled={!runtime.allowed}
              onClick={() => setSheet("import")}
            >
              {t("Import")}
            </Button>
          </Tip>
          <Tip label={runtime.reason}>
            <Button
              variant="outline"
              disabled={!runtime.allowed}
              onClick={() => setSheet("import-ca")}
            >
              {t("Import client CA")}
            </Button>
          </Tip>
          <Tip label={runtime.reason}>
            <Button disabled={!runtime.allowed} onClick={() => setSheet("issue")}>
              {t("Issue certificate")}
            </Button>
          </Tip>
        </div>
      </div>
      {problem != null && <ErrorState error={problem} />}
      {all.length > 0 && (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2">
            <input
              aria-label={t("Filter by name or SAN")}
              placeholder={t("Filter by name or SAN")}
              className="h-9 w-64 rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <select
              aria-label={t("Expiry")}
              className="h-9 rounded-md border border-input bg-card px-3 text-sm"
              value={expiryFilter}
              onChange={(e) => setExpiryFilter(e.target.value as typeof expiryFilter)}
            >
              <option value="">{t("Any expiry")}</option>
              <option value="30">{t("30 days or less")}</option>
              <option value="14">{t("14 days or less")}</option>
              <option value="7">{t("7 days or less, or expired")}</option>
            </select>
            <select
              aria-label={t("Issuer")}
              className="h-9 rounded-md border border-input bg-card px-3 text-sm"
              value={issuer}
              onChange={(e) => setIssuer(e.target.value)}
            >
              <option value="">{t("Any issuer")}</option>
              {[...new Set(all.map((c) => c.source))].map((s) => (
                <option key={s} value={s}>
                  {t(SOURCE[s] ?? s)}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {t("Expiry:")}
            <Badge tone="neutral">
              <Clock className="size-3" aria-hidden /> {t("30 days or less")}
            </Badge>
            <Badge tone="degraded">
              <AlertTriangle className="size-3" aria-hidden /> {t("14 or less")}
            </Badge>
            <Badge tone="down">
              <XCircle className="size-3" aria-hidden /> {t("7 or less, or expired")}
            </Badge>
          </div>
        </div>
      )}
      <div className="overflow-x-auto rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton cols={7} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState
            title={t("No certificates yet")}
            body={t(
              "Import a certificate and key, or issue one with ACME or Cloudflare Origin CA.",
            )}
            action={
              runtime.allowed ? (
                <Button onClick={() => setSheet("import")}>{t("Import a certificate")}</Button>
              ) : undefined
            }
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["Name", "Issuer", "SANs", "Expires", "Renewal", "Used by", "Actions"].map((h) => (
                  <th key={h} className="px-4 py-2.5 font-medium whitespace-nowrap">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {rows.map((c) => {
                const ex = expiryStatus(c.not_after);
                const users = usedBy.get(c.id) ?? [];
                return (
                  <tr key={c.id}>
                    <td className="px-4 py-3">
                      <span className="flex items-center gap-2">
                        {c.draft_status !== "unchanged" && <DraftMarker status={c.draft_status} />}
                        <button
                          type="button"
                          aria-label={t("Where {name} is used", { name: c.name })}
                          className={cn(
                            "text-left font-medium whitespace-nowrap text-primary hover:underline",
                            c.draft_status === "deleted" && "line-through",
                          )}
                          onClick={() => setDetail(c.id)}
                        >
                          {c.name}
                        </button>
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {t(SOURCE[c.source] ?? c.source)}
                      {c.purpose === "client_ca" && (
                        <span className="ml-1 text-xs text-muted-foreground">
                          · {t("client CA")}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 font-mono text-xs">{c.sans.join(", ")}</td>
                    <td className="px-4 py-3">
                      <span className="flex items-center gap-2 whitespace-nowrap">
                        <ExpiryPill ex={ex} />
                        <span className="whitespace-nowrap text-muted-foreground">
                          {shortDate(c.not_after)}
                        </span>
                      </span>
                    </td>
                    <td className="px-4 py-3">{renewalLabel(c)}</td>
                    <td className="px-4 py-3">{users.length ? users.join(", ") : t("none")}</td>
                    <td className="px-4 py-3">
                      <span className="flex gap-1">
                        <Tip label={runtime.reason}>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={!runtime.allowed || c.source === "upload"}
                            onClick={() => void renew(c.id)}
                          >
                            {t("Renew now")}
                          </Button>
                        </Tip>
                        <Tip
                          label={
                            users.length
                              ? t("Used by {n} services; remove it from them first.", {
                                  n: users.length,
                                })
                              : write.reason
                          }
                        >
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={
                              !write.allowed || users.length > 0 || c.draft_status === "deleted"
                            }
                            onClick={() =>
                              setDeleting({ id: c.id, name: c.name, version: c.version })
                            }
                          >
                            {t("Delete")}
                          </Button>
                        </Tip>
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        {t(
          "Issuing and renewing run as jobs, then open an ordinary change set (author system:certs) that is validated, canaried and revertible. Delete is blocked while any virtual service, live or drafted, uses the certificate.",
        )}
      </p>
      {sheet === "import" && <ImportSheet tenant={tenant} onClose={() => setSheet(null)} />}
      {sheet === "import-ca" && <ImportCaSheet tenant={tenant} onClose={() => setSheet(null)} />}
      {sheet === "issue" && <IssueSheet tenant={tenant} onClose={() => setSheet(null)} />}
      {detail && (
        <UsageSheet
          tenant={tenant}
          cert={rows.find((c) => c.id === detail)}
          services={(services.data?.items ?? []).filter((v) => v.certificate_ids.includes(detail))}
          onClose={() => setDetail(null)}
          onReplace={() => {
            const c = rows.find((x) => x.id === detail);
            if (c) setReplacing({ id: c.id, name: c.name });
          }}
        />
      )}
      {replacing && (
        <ImportSheet tenant={tenant} replace={replacing} onClose={() => setReplacing(null)} />
      )}
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={t("Delete certificate {name}?", { name: deleting?.name ?? "" })}
        confirmLabel={t("Add delete to draft")}
        busy={del.isPending}
        onConfirm={async () => {
          if (!deleting) return;
          try {
            await del.mutateAsync({ action: "delete", id: deleting.id, version: deleting.version });
          } catch (e) {
            setProblem(e);
          } finally {
            setDeleting(null);
          }
        }}
      >
        <p>
          {t(
            "The certificate record is removed when the change set is applied. The key stays in OpenBao until it is cleaned up.",
          )}
        </p>
      </ConfirmDialog>
    </div>
  );
}
