import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, unwrap } from "@/api/client";
import { useCan } from "@/auth/session";
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

function ImportSheet({ tenant, onClose }: { tenant: string; onClose: () => void }) {
  const ensure = useEnsureDraft(tenant);
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [pem, setPem] = useState("");
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);
  const seen = looksLikePem(pem);
  async function submit() {
    setBusy(true);
    setProblem(null);
    try {
      const cs = await ensure.mutateAsync();
      unwrap(
        await api.POST("/api/v1/tenants/{t}/certificates/import", {
          params: { path: { t: tenant }, query: { change_set: cs } },
          body: { name, pem },
        }),
      );
      setPem("");
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
          <Button disabled={busy || !name || !seen.cert || !seen.key} onClick={() => void submit()}>
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
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Certificate chain and private key (PEM)")}
        <textarea
          aria-label={t("Certificate and key PEM")}
          className="h-56 w-full rounded-md border bg-card p-3 font-mono text-xs"
          spellCheck={false}
          autoComplete="off"
          value={pem}
          onChange={(e) => setPem(e.target.value)}
        />
      </label>
      <p className="text-xs text-muted-foreground">
        {t(
          "Leaf first, then intermediates, then an unencrypted key. Sent once, stored in OpenBao, never shown again.",
        )}
      </p>
      <ul className="space-y-1 text-sm">
        <li>
          {seen.cert ? "✓" : "○"} {t("Certificate found")}
        </li>
        <li>
          {seen.key ? "✓" : "○"} {t("Private key found")}
        </li>
      </ul>
      <p className="text-xs text-muted-foreground">
        {t(
          "SANs and expiry are read from the certificate. Importing adds the certificate to your draft.",
        )}
      </p>
    </Sheet>
  );
}

function IssueSheet({ tenant, onClose }: { tenant: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [source, setSource] = useState<"acme" | "cloudflare_origin">("acme");
  const [name, setName] = useState("");
  const [sans, setSans] = useState("");
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
          body: { name, source, sans: sans.split(/[\s,]+/).filter(Boolean) },
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
            <Button disabled={busy || !name || !sans.trim()} onClick={() => void submit()}>
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
          <label className="block space-y-1.5 text-sm font-medium">
            {t("Names (SANs)")}
            <input
              aria-label={t("Names (SANs)")}
              className={cn(INPUT, "font-mono")}
              placeholder="docs.example.com, www.example.com"
              value={sans}
              onChange={(e) => setSans(e.target.value)}
            />
            <span className="block text-xs font-normal text-muted-foreground">
              {t("Each name must be in a Cloudflare zone this tenant can edit.")}
            </span>
          </label>
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
                        <span
                          className={cn(
                            "font-medium",
                            c.draft_status === "deleted" && "line-through",
                          )}
                        >
                          {c.name}
                        </span>
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
