// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Admin > Upgrades > Upgrade bundles: upload a signed bundle (or fetch the one the release feed
 * lists), see that it verified and what applying it involves, and download it for another node.
 * The controller never applies a bundle itself; that is `syctl upgrade apply` on each node
 * (ADR 0205).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { ApiError, api, rawUpload, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { ErrorState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { bytes } from "@/features/admin/ControllerMembers";
import { t } from "@/lib/i18n";

type Staged = components["schemas"]["StagedBundleRead"];

/** The command an operator runs on each controller node for a staged bundle. */
export function applyCommand(b: Pick<Staged, "version">): string {
  return `syctl upgrade apply switchyard-${b.version}.sybundle --trust <key id>=<public key> --compose-file COMPOSE --env-file NODE.ENV --backup-target NAME`;
}

function Plan({ name }: { name: string }) {
  const q = useQuery({
    queryKey: ["admin", "bundles", name],
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/system/bundles/{bundle}", { params: { path: { bundle: name } } }),
      ),
  });
  if (q.isPending) return <p className="p-3 text-sm text-muted-foreground">{t("Loading…")}</p>;
  if (q.isError) return <ErrorState error={q.error} />;
  const p = q.data;
  const notes = (["breaking", "action_required", "deprecated", "new"] as const).filter(
    (g) => (p.bundle.notes[g] ?? []).length > 0,
  );
  return (
    <div className="space-y-3 border-t bg-muted/20 p-4 text-sm">
      {p.warnings.map((w) => (
        <p
          key={w}
          role="note"
          className="rounded-md bg-status-degraded-bg p-3 text-status-degraded"
        >
          {w}
        </p>
      ))}
      {notes.map((g) => (
        <div key={g}>
          <div className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
            {t(g.replace("_", " "))}
          </div>
          <ul className="list-disc pl-5">
            {(p.bundle.notes[g] ?? []).map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        </div>
      ))}
      <div>
        <div className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
          {t("Steps")}
        </div>
        <ol className="list-decimal pl-5">
          {p.steps.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ol>
      </div>
      <div>
        <div className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
          {t("On each controller node")}
        </div>
        <code className="block overflow-x-auto rounded-md bg-muted p-2 text-xs">
          {applyCommand(p.bundle)}
        </code>
      </div>
    </div>
  );
}

export function Bundles({ latest }: { latest: string | null }) {
  const qc = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [remove, setRemove] = useState<Staged | null>(null);
  const list = useQuery({
    queryKey: ["admin", "bundles"],
    queryFn: async () => unwrap(await api.GET("/api/v1/system/bundles")),
  });
  const refresh = () => void qc.invalidateQueries({ queryKey: ["admin", "bundles"] });
  const upload = useMutation({
    mutationFn: async (file: File) => rawUpload<Staged>("/api/v1/system/bundles", file),
    onSuccess: refresh,
  });
  const fetchLatest = useMutation({
    mutationFn: async (version: string) =>
      unwrap(await api.POST("/api/v1/system/bundles/fetch", { body: { version } })),
    onSuccess: refresh,
  });
  const del = useMutation({
    mutationFn: async (name: string) =>
      unwrap(
        await api.DELETE("/api/v1/system/bundles/{bundle}", { params: { path: { bundle: name } } }),
      ),
    onSettled: () => {
      setRemove(null);
      refresh();
    },
  });
  const have = new Set(list.data?.map((b) => b.version));
  const error = upload.error ?? fetchLatest.error ?? del.error;
  return (
    <section className="overflow-hidden rounded-lg border bg-card">
      <header className="flex items-center gap-3 border-b px-4 py-3">
        <h2 className="text-base font-semibold">{t("Upgrade bundles")}</h2>
        <span className="ml-auto flex items-center gap-2">
          {latest && !have.has(latest) && (
            <Button
              size="sm"
              variant="outline"
              disabled={fetchLatest.isPending}
              onClick={() => fetchLatest.mutate(latest)}
            >
              {fetchLatest.isPending
                ? t("Downloading…")
                : t("Download {v} from the feed", { v: latest })}
            </Button>
          )}
          <input
            ref={input}
            type="file"
            accept=".sybundle"
            aria-label={t("Bundle file")}
            className="sr-only"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) upload.mutate(file);
              e.target.value = "";
            }}
          />
          <Button size="sm" disabled={upload.isPending} onClick={() => input.current?.click()}>
            {upload.isPending ? t("Uploading and checking…") : t("Upload a bundle…")}
          </Button>
        </span>
      </header>
      <div className="space-y-2 p-4 text-sm">
        {error && <ErrorState error={error} onRetry={undefined} />}
        <p className="text-muted-foreground">
          {t(
            "A bundle is kept only after its signature and every file's checksum verify. The controller does not apply it: run the command shown under it on each controller node, one at a time.",
          )}
        </p>
        {error instanceof ApiError && error.status === 413 && (
          <p>{t("The proxy in front refused a file this large; see docs/upgrades.md.")}</p>
        )}
      </div>
      {list.data && list.data.length === 0 && (
        <p className="border-t p-4 text-sm text-muted-foreground">{t("No bundles staged.")}</p>
      )}
      <ul className="divide-y border-t">
        {list.data?.map((b) => (
          <li key={b.name}>
            <div className="flex items-center gap-3 px-4 py-3 text-sm">
              <span className="font-medium">{b.version}</span>
              <StatusPill status="healthy">{t("Verified")}</StatusPill>
              <span className="text-muted-foreground">
                {bytes(b.size_bytes)} · {t("signed by {key}", { key: b.key_id })}
              </span>
              <span className="ml-auto flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => setOpen(open === b.name ? null : b.name)}
                >
                  {open === b.name ? t("Hide plan") : t("Plan")}
                </Button>
                <a
                  className="inline-flex h-8 items-center rounded-md border px-3 text-sm hover:bg-muted"
                  href={`/api/v1/system/bundles/${b.name}/file`}
                  download
                >
                  {t("Download")}
                </a>
                <Button size="sm" variant="outline" onClick={() => setRemove(b)}>
                  {t("Remove")}
                </Button>
              </span>
            </div>
            {open === b.name && <Plan name={b.name} />}
          </li>
        ))}
      </ul>
      <ConfirmDialog
        open={remove !== null}
        onOpenChange={(o) => !o && setRemove(null)}
        title={t("Remove {v} from the controller?", { v: remove?.version ?? "" })}
        confirmLabel={t("Remove")}
        busy={del.isPending}
        onConfirm={() => remove && del.mutate(remove.name)}
      >
        <p>{t("Only the staged copy is removed. The release feed can fetch it again.")}</p>
      </ConfirmDialog>
    </section>
  );
}
