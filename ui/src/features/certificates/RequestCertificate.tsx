/**
 * Request certificate (operator): ACME provider, names, DNS-01 or HTTP-01, renewal. The request is
 * checked on the server as you type (`issue/check`), then runs as a job whose stages are shown
 * here; a failure quotes the CA's error and offers Retry, Edit request and Open job.
 */
import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { CheckCircle2, Clock, Loader2, XCircle } from "lucide-react";
import { useEffect, useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { ChipInput, validateHostname } from "@/components/sy/ChipInput";
import { ErrorState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Combobox } from "@/components/ui/combobox";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

type IssueIn = components["schemas"]["CertIssueIn"];
type Candidate = components["schemas"]["DnsCandidateRead"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring";

const STAGE_LABEL: Record<string, string> = {
  order: "Order placed",
  validate: "Challenge published and validated by the CA",
  finalize: "Certificate issued",
  store: "Stored in OpenBao",
  "change set": "Added to a system:certs change set",
};

/** Credentials that cover every name (longest zone first, as ranked for the first name). */
export function commonCandidates(per: { name: string; candidates: Candidate[] }[]): Candidate[] {
  const [first, ...rest] = per;
  if (!first) return [];
  return first.candidates.filter((c) => rest.every((r) => r.candidates.some((x) => x.id === c.id)));
}

function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setV(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return v;
}

function useJob(tenant: string, id: string | null) {
  return useQuery({
    queryKey: ["t", tenant, "jobs", "issue", id],
    enabled: id !== null,
    refetchInterval: (q) =>
      q.state.data && ["succeeded", "failed", "dead", "cancelled"].includes(q.state.data.state)
        ? false
        : 1500,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/jobs/{job_id}", {
          params: { path: { t: tenant, job_id: id! } },
        }),
      ),
  });
}

function Progress({
  tenant,
  jobId,
  summary,
  onRetry,
  onEdit,
  onClose,
}: {
  tenant: string;
  jobId: string;
  summary: { names: string[]; provider: string };
  onRetry: () => void;
  onEdit: () => void;
  onClose: () => void;
}) {
  const job = useJob(tenant, jobId);
  const j = job.data;
  const failed = j && (j.state === "failed" || j.state === "dead" || j.state === "cancelled");
  const done = j?.state === "succeeded";
  const stages = j?.stages ?? [];
  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={
        failed ? t("Request failed") : done ? t("Certificate issued") : t("Requesting certificate")
      }
      footer={
        <>
          {failed && (
            <Button variant="ghost" onClick={onEdit}>
              {t("Edit request")}
            </Button>
          )}
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Close")}
          </Button>
          {failed && <Button onClick={onRetry}>{t("Retry")}</Button>}
        </>
      }
    >
      <dl className="grid grid-cols-[6rem_1fr] gap-y-1 text-sm">
        <dt className="text-muted-foreground">{t("Names")}</dt>
        <dd className="font-mono text-xs">{summary.names.join(", ")}</dd>
        <dt className="text-muted-foreground">{t("Provider")}</dt>
        <dd>{summary.provider}</dd>
      </dl>
      {job.isError && <ErrorState error={job.error} onRetry={() => void job.refetch()} />}
      <ol className="divide-y" aria-label={t("Progress")}>
        {stages.length === 0 && !failed && (
          <li className="flex items-center gap-2 py-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" aria-hidden /> {t("Queued")}
          </li>
        )}
        {stages.map((s) => (
          <li key={s.name} className="flex items-start gap-2.5 py-2.5 text-sm">
            {s.state === "succeeded" ? (
              <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-status-healthy" aria-hidden />
            ) : s.state === "failed" ? (
              <XCircle className="mt-0.5 size-4 shrink-0 text-status-down" aria-hidden />
            ) : s.state === "running" ? (
              <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin" aria-hidden />
            ) : (
              <Clock className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
            )}
            <span>
              <span className={cn(s.state === "failed" && "font-semibold")}>
                {t(STAGE_LABEL[s.name] ?? s.name)}
              </span>
              {s.state === "pending" && (
                <span className="block text-xs text-muted-foreground">{t("not reached")}</span>
              )}
            </span>
          </li>
        ))}
      </ol>
      {failed && (
        <div
          role="alert"
          className="space-y-1.5 rounded-lg border border-status-down/40 bg-status-down-bg p-3 text-sm"
        >
          <p className="font-semibold">{t("The request failed.")}</p>
          <p className="font-mono text-xs break-words">{j?.error ?? t("No error was recorded.")}</p>
          <p className="text-xs">
            {t(
              "The job log says whether the challenge records were removed. Nothing was added to a change set. Fix the cause, then retry; retrying soon after many failures can hit the CA's rate limits.",
            )}
          </p>
        </div>
      )}
      {done && (
        <p role="status" className="rounded-md bg-muted p-3 text-sm">
          {t(
            "The certificate is stored. A system:certs change set adds it; it follows approval like any other.",
          )}{" "}
          {j?.change_set_id && (
            <Link
              to="/t/$tenant/review/$cs"
              params={{ tenant, cs: j.change_set_id }}
              className="text-primary hover:underline"
            >
              {t("Open the change set")}
            </Link>
          )}
        </p>
      )}
      <p className="text-xs text-muted-foreground">
        <Link to="/t/$tenant/jobs" params={{ tenant }} className="text-primary hover:underline">
          {t("Open job")}
        </Link>{" "}
        {t("for the full log.")}
      </p>
    </Sheet>
  );
}

export function RequestCertificate({
  tenant,
  onClose,
  onDone,
}: {
  tenant: string;
  onClose: () => void;
  onDone: () => void;
}) {
  const [source, setSource] = useState<"acme" | "cloudflare_origin">("acme");
  const [name, setName] = useState("");
  const [sans, setSans] = useState<string[]>([]);
  const [provider, setProvider] = useState<string>("");
  const [challenge, setChallenge] = useState<"dns-01" | "http-01" | "tls-alpn-01">("dns-01");
  const [cred, setCred] = useState<string | null>(null);
  const [auto, setAuto] = useState(true);
  const [days, setDays] = useState("30");
  const [submitting, setSubmitting] = useState(false);
  const [problem, setProblem] = useState<unknown>(null);
  const [run, setRun] = useState<string | null>(null);

  const providers = useQuery({
    queryKey: ["acme-providers"],
    queryFn: async () => unwrap(await api.GET("/api/v1/acme-providers")),
    enabled: source === "acme",
  });
  const enabled = (providers.data ?? []).filter((p) => p.enabled);
  const effProvider = provider || enabled.find((p) => p.default)?.name || enabled[0]?.name || "";

  const dns = source === "acme" && challenge === "dns-01";
  const debouncedSans = useDebounced(sans, 300);
  const match = useQuery({
    queryKey: ["t", tenant, "dns-match", debouncedSans],
    enabled: dns && debouncedSans.length > 0,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/dns-providers/match", {
          params: { path: { t: tenant }, query: { names: debouncedSans } },
        }),
      ),
  });
  const common = commonCandidates(match.data ?? []);
  const effCred = cred && common.some((c) => c.name === cred) ? cred : (common[0]?.name ?? "");
  const noCred = dns && sans.length > 0 && match.isSuccess && common.length === 0;
  const uncovered = (match.data ?? []).filter((m) => m.candidates.length === 0).map((m) => m.name);

  const body: IssueIn = {
    name,
    sans,
    source,
    ...(source === "acme"
      ? {
          acme_provider: effProvider || null,
          challenge,
          dns_provider: dns ? effCred || null : null,
          renew_policy: auto ? "auto" : "manual",
          renew_before_days: Number(days) || 30,
        }
      : { renew_policy: auto ? "auto" : "manual" }),
  };
  const checkBody = useDebounced(JSON.stringify(body), 400);
  const check = useQuery({
    queryKey: ["t", tenant, "issue-check", checkBody],
    enabled: name !== "" && sans.length > 0 && (source !== "acme" || !dns || !match.isPending),
    queryFn: async () =>
      unwrap(
        await api.POST("/api/v1/tenants/{t}/certificates/issue/check", {
          params: { path: { t: tenant } },
          body: JSON.parse(checkBody) as IssueIn,
        }),
      ),
  });
  const problems = check.data?.problems ?? [];
  const warnings = check.data?.warnings ?? [];
  const at = (prefix: string) => problems.filter((p) => p.path.startsWith(prefix));
  const general = problems.filter((p) => !/^(sans|acme_provider|dns_provider)/.test(p.path));

  async function submit() {
    setSubmitting(true);
    setProblem(null);
    try {
      const r = unwrap(
        await api.POST("/api/v1/tenants/{t}/certificates/issue", {
          params: { path: { t: tenant } },
          body,
        }),
      );
      setRun(r.job_id);
      onDone();
    } catch (e) {
      setProblem(e);
    } finally {
      setSubmitting(false);
    }
  }

  if (run)
    return (
      <Progress
        tenant={tenant}
        jobId={run}
        summary={{
          names: sans,
          provider:
            source === "acme"
              ? `${effProvider} · ${challenge.toUpperCase()}${dns && effCred ? ` · ${effCred}` : ""}`
              : "Cloudflare Origin CA",
        }}
        onRetry={() => {
          setRun(null);
          void submit();
        }}
        onEdit={() => setRun(null)}
        onClose={onClose}
      />
    );

  const wildcard = sans.some((s) => s.startsWith("*."));
  const blocked =
    !name ||
    sans.length === 0 ||
    submitting ||
    problems.length > 0 ||
    check.isFetching ||
    (source === "acme" &&
      (!effProvider || noCred || (challenge !== "dns-01" && (wildcard || sans.length > 1))));
  const why = noCred
    ? t("Add a DNS credential that covers every name, or remove the names it doesn't cover.")
    : problems.length > 0
      ? t("Fix the problems above to request.")
      : undefined;

  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={t("Request certificate")}
      footer={
        <>
          {blocked && why && (
            <span id="request-why" className="text-xs text-muted-foreground">
              {why}
            </span>
          )}
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            disabled={blocked}
            aria-describedby={blocked && why ? "request-why" : undefined}
            onClick={() => void submit()}
          >
            {t("Request")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">{t("Issuer")}</legend>
        <div className="grid grid-cols-2 gap-3">
          {(
            [
              ["acme", "ACME", "Publicly trusted, from the provider you pick."],
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
      {source === "acme" && (
        <div className="space-y-1.5">
          <div className="text-sm font-medium">{t("ACME provider")}</div>
          <Combobox
            aria-label={t("ACME provider")}
            value={effProvider}
            onChange={setProvider}
            options={enabled.map((p) => ({
              value: p.name,
              label: p.default ? `${p.name} (${t("default")})` : p.name,
            }))}
          />
          {providers.isSuccess && enabled.length === 0 && (
            <p role="alert" className="text-xs text-status-down">
              {t(
                "No ACME provider is enabled. A system administrator adds one under Admin > ACME providers.",
              )}
            </p>
          )}
          {at("acme_provider").map((p) => (
            <p key={p.message} role="alert" className="text-xs text-status-down">
              {p.message}
            </p>
          ))}
        </div>
      )}
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
        <p className="text-xs text-muted-foreground">
          {t("Press Enter or comma to add a name. Wildcards (*.example.com) need DNS-01.")}
        </p>
        {at("sans").map((p) => (
          <p key={p.path + p.message} role="alert" className="text-xs text-status-down">
            {p.path}: {p.message}
          </p>
        ))}
      </div>
      {source === "acme" && (
        <>
          <fieldset className="space-y-1.5">
            <legend className="text-sm font-medium">{t("Validation")}</legend>
            <div role="radiogroup" className="flex gap-6 text-sm">
              {(
                [
                  ["dns-01", "DNS-01"],
                  ["http-01", "HTTP-01"],
                  ["tls-alpn-01", "TLS-ALPN-01"],
                ] as const
              ).map(([k, label]) => (
                <label key={k} className="flex items-center gap-1.5">
                  <input
                    type="radio"
                    name="challenge"
                    checked={challenge === k}
                    onChange={() => setChallenge(k)}
                  />
                  {label}
                </label>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">
              {challenge === "dns-01"
                ? t(
                    "Publishes a TXT record through one of your DNS credentials. Works for wildcards.",
                  )
                : challenge === "http-01"
                  ? t(
                      "One name, no wildcard. It must resolve to a VIP of this tenant with an HTTP service on port 80 (no allow list).",
                    )
                  : t(
                      "{name} already serves HTTPS. While the CA validates it, the engines show a temporary certificate for a few seconds, so visitors in that moment may see a certificate warning. Use DNS-01 for busy names, or request it at a quiet time.",
                      { name: sans[0] ?? t("The name") },
                    )}
            </p>
          </fieldset>
          {dns && (
            <div className="space-y-1.5">
              <div className="text-sm font-medium">{t("DNS credential")}</div>
              <Combobox
                aria-label={t("DNS credential")}
                value={effCred}
                onChange={setCred}
                disabled={common.length === 0}
                placeholder={
                  sans.length === 0 ? t("Add a name first") : t("None covers every name")
                }
                options={common.map((c) => ({
                  value: c.name,
                  label: c.zone ? `${c.name} · ${c.zone}` : c.name,
                }))}
              />
              {noCred && (
                <p role="alert" className="text-xs text-status-down">
                  {uncovered.length > 0
                    ? t("No credential of this tenant covers: {names}.", {
                        names: uncovered.join(", "),
                      })
                    : t(
                        "No single credential covers every name. A certificate uses one credential.",
                      )}
                </p>
              )}
              {at("dns_provider").map((p) => (
                <p key={p.message} role="alert" className="text-xs text-status-down">
                  {p.message}
                </p>
              ))}
              <p className="text-xs text-muted-foreground">
                {t(
                  "Lists only this tenant's credentials whose zones cover every name, longest zone first.",
                )}
              </p>
            </div>
          )}
        </>
      )}
      {warnings.map((w) => (
        <p key={w.path + w.message} role="note" className="text-xs text-status-degraded">
          {w.message}
        </p>
      ))}
      {general.map((p) => (
        <p key={p.path + p.message} role="alert" className="text-xs text-status-down">
          {p.message}
        </p>
      ))}
      <div className="flex items-center gap-3">
        <Switch aria-label={t("Renew automatically")} checked={auto} onCheckedChange={setAuto} />
        <span className="text-sm">{t("Renew automatically")}</span>
        {auto && source === "acme" && (
          <label className="ml-auto flex items-center gap-2 text-sm">
            {t("Days before expiry")}
            <input
              aria-label={t("Days before expiry")}
              type="number"
              min={1}
              className={cn(INPUT, "w-20")}
              value={days}
              onChange={(e) => setDays(e.target.value)}
            />
          </label>
        )}
      </div>
      <p className="rounded-md bg-muted p-3 text-sm">
        {t(
          "Runs as a job. The key is generated on the controller and stored in OpenBao. When the certificate is ready, a system:certs change set adds it; it waits for approval like any other.",
        )}
      </p>
    </Sheet>
  );
}
