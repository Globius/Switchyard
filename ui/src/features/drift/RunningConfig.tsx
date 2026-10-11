/** "View running config": what an engine runs right now, next to what the controller wants. */
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import { ConfigEditor } from "@/components/sy/ConfigEditor";
import { StatusPill } from "@/components/sy/StatusPill";
import { ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

function useRunningConfig(tenant: string, engine: string, enabled: boolean) {
  return useQuery({
    queryKey: ["t", tenant, "engines", engine, "running-config"],
    enabled,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/tenants/{t}/engines/{engine}/running-config", {
          params: { path: { t: tenant, engine } },
        }),
      ),
  });
}

const short = (h: string | null | undefined) => (h ? `${h.slice(0, 12)}…` : "—");

function Match({ ok }: { ok: boolean }) {
  return ok ? (
    <StatusPill status="healthy">{t("Matches")}</StatusPill>
  ) : (
    <StatusPill status="degraded">{t("Differs")}</StatusPill>
  );
}

function Body({ tenant, engine }: { tenant: string; engine: string }) {
  const q = useRunningConfig(tenant, engine, true);
  if (q.isPending) return <TableSkeleton rows={4} cols={2} />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const r = q.data;
  const hp = r.haproxy;
  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        {t("Fetched {time} from the engine. Reported only; nothing is changed.", {
          time: new Date(r.fetched_at).toLocaleTimeString(),
        })}
      </p>
      {!r.reachable && (
        <p
          role="alert"
          className="rounded-md border border-status-down/40 bg-status-down-bg p-3 text-sm"
        >
          {t("The engine could not be reached")}
          {r.error ? `: ${r.error}` : ""}
        </p>
      )}
      {hp && (
        <section className="overflow-hidden rounded-lg border">
          <header className="flex flex-wrap items-center gap-3 border-b bg-muted/60 px-4 py-2 text-xs">
            <span className="font-mono">haproxy.cfg</span>
            <Match ok={hp.matches_desired} />
            <span className="ml-auto text-muted-foreground">
              {t("running")} <span className="font-mono">{short(hp.fingerprint)}</span> ·{" "}
              {t("desired")} <span className="font-mono">{short(hp.desired_fingerprint)}</span>
            </span>
          </header>
          <ConfigEditor
            content={hp.content}
            label="haproxy.cfg"
            fallback={
              <pre className="max-h-96 overflow-auto p-3 font-mono text-xs leading-5">
                {hp.content}
              </pre>
            }
          />
        </section>
      )}
      {r.agent && (
        <section className="space-y-2">
          <div className="flex items-center gap-2">
            <h3 className="text-sm font-semibold">{t("Agent files")}</h3>
            <span className="text-xs text-muted-foreground">
              {t("bundle")} <span className="font-mono">{short(r.agent.bundle_id)}</span>
              {" · "}
              {t("desired")} <span className="font-mono">{short(r.agent.desired_bundle_id)}</span>
            </span>
          </div>
          <p className="text-xs text-muted-foreground">
            {t("The agent reports hashes only, never file contents.")}
          </p>
          <table className="w-full overflow-hidden rounded-md border text-[13px]">
            <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["File", "Running", "Desired", ""].map((h, i) => (
                  <th key={i} className="px-3 py-2 font-medium">
                    {t(h)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y">
              {r.agent.files.map((f) => (
                <tr key={f.path} className={cn(!f.matches_desired && "bg-draft/10")}>
                  <td className="px-3 py-2 font-mono text-xs">{f.path}</td>
                  <td className="px-3 py-2 font-mono text-xs">{short(f.sha256)}</td>
                  <td className="px-3 py-2 font-mono text-xs">{short(f.desired_sha256)}</td>
                  <td className="px-3 py-2">
                    <Match ok={f.matches_desired} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
      {r.reachable && !hp && !r.agent && (
        <p className="text-sm text-muted-foreground">
          {t("The engine reported no configuration.")}
        </p>
      )}
    </div>
  );
}

export function RunningConfigButton({
  tenant,
  engine,
}: {
  tenant: string;
  engine: { id: string; name: string };
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" variant="ghost" onClick={() => setOpen(true)}>
        {t("View running config")}
      </Button>
      {open && (
        <Sheet
          open
          onOpenChange={(o) => !o && setOpen(false)}
          title={t("Running config of {name}", { name: engine.name })}
          footer={
            <Button className="ml-auto" onClick={() => setOpen(false)}>
              {t("Close")}
            </Button>
          }
        >
          <Body tenant={tenant} engine={engine.id} />
        </Sheet>
      )}
    </>
  );
}
