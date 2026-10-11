import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { useCan } from "@/auth/session";
import { ChipInput, validateHostname } from "@/components/sy/ChipInput";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { DraftMarker, expiryStatus, StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Tip } from "@/components/ui/tooltip";
import { useSaveOp } from "@/features/objects/save";
import { useDraft } from "@/features/shell/draft";
import { useEnsureDraft } from "@/features/shell/draft-write";
import { useCertificates } from "@/features/vs/queries";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

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

function ImportSheet({ tenant, onClose }: { tenant: string; onClose: () => void }) {
  const ensure = useEnsureDraft(tenant);
  const qc = useQueryClient();
  const [name, setName] = useState("");
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
      unwrap(
        await api.POST("/api/v1/tenants/{t}/certificates/import", {
          params: { path: { t: tenant }, query: { change_set: cs } },
          body:
            mode === "pem"
              ? { name, pem: buildBundle(cert, chain, key) }
              : { name, pkcs12_b64: pfx!.b64, password },
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
      title={t("Import certificate")}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            disabled={busy || !name || (mode === "pem" ? !certOk || !chainOk || !keyOk : !pfx)}
            onClick={() => void submit()}
          >
            {t("Add to draft")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Name")}
        <input
          aria-label={t("Name")}
          className={INPUT}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
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
function UsageSheet({
  tenant,
  cert,
  services,
  onClose,
}: {
  tenant: string;
  cert: CertRow | undefined;
  services: ServiceRow[];
  onClose: () => void;
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
        <Button variant="ghost" className="ml-auto" onClick={onClose}>
          {t("Close")}
        </Button>
      }
    >
      <dl className="grid grid-cols-[7rem_1fr] gap-y-2 text-sm">
        <dt className="text-muted-foreground">{t("Source")}</dt>
        <dd>{t(SOURCE[cert.source] ?? cert.source)}</dd>
        <dt className="text-muted-foreground">{t("Names")}</dt>
        <dd className="font-mono text-xs">{cert.sans.join(", ")}</dd>
        <dt className="text-muted-foreground">{t("Expires")}</dt>
        <dd className="flex items-center gap-2">
          <StatusPill status={ex.status}>{ex.label}</StatusPill>
          {new Date(cert.not_after).toLocaleDateString()}
        </dd>
        <dt className="text-muted-foreground">{t("Renewal")}</dt>
        <dd>{cert.renew_policy === "auto" ? t("Auto") : t("Manual")}</dd>
        <dt className="text-muted-foreground">{t("Key")}</dt>
        <dd className="font-mono text-xs">{cert.secret_ref}</dd>
      </dl>
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
                  source === k && "border-primary",
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
  const [sheet, setSheet] = useState<"import" | "issue" | null>(null);
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
  const rows = [...(list.data?.items ?? [])].sort((a, b) => a.not_after.localeCompare(b.not_after));

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
            <Button disabled={!runtime.allowed} onClick={() => setSheet("issue")}>
              {t("Issue certificate")}
            </Button>
          </Tip>
        </div>
      </div>
      {problem != null && <ErrorState error={problem} />}
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
                {["Name", "Source", "SANs", "Expires", "Renewal", "Used by", "Actions"].map((h) => (
                  <th key={h} className="px-4 py-2.5 font-medium">
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
                            "font-medium text-primary hover:underline",
                            c.draft_status === "deleted" && "line-through",
                          )}
                          onClick={() => setDetail(c.id)}
                        >
                          {c.name}
                        </button>
                      </span>
                    </td>
                    <td className="px-4 py-3">{t(SOURCE[c.source] ?? c.source)}</td>
                    <td className="px-4 py-3 font-mono text-xs">{c.sans.join(", ")}</td>
                    <td className="px-4 py-3">
                      <span className="flex items-center gap-2">
                        <StatusPill status={ex.status}>{ex.label}</StatusPill>
                        <span className="text-muted-foreground">
                          {new Date(c.not_after).toLocaleDateString()}
                        </span>
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {c.renew_policy === "auto" ? t("Auto") : t("Manual")}
                    </td>
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
      {sheet === "issue" && <IssueSheet tenant={tenant} onClose={() => setSheet(null)} />}
      {detail && (
        <UsageSheet
          tenant={tenant}
          cert={rows.find((c) => c.id === detail)}
          services={(services.data?.items ?? []).filter((v) => v.certificate_ids.includes(detail))}
          onClose={() => setDetail(null)}
        />
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
