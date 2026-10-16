// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Admin > Upgrades > Configuration backups: the destinations backups are written to. Add, test
 * and remove them (ADR 0205). Every credential is an OpenBao `secret_ref`; the form never takes a
 * secret value.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { StatusPill } from "@/components/sy/StatusPill";
import { ErrorState } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";

export type BackupTarget = components["schemas"]["BackupTargetConfig"];
type Kind = "local" | "s3" | "sftp" | "ftp";

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60";

/** The fields each kind asks for: `key`, label, and whether it must be filled in. */
const FIELDS: Record<Kind, { key: string; label: string; required: boolean; hint?: string }[]> = {
  local: [{ key: "path", label: "Directory under the backup root", required: false }],
  s3: [
    { key: "endpoint", label: "Endpoint", required: true, hint: "https://s3.example.com" },
    { key: "region", label: "Region", required: false, hint: "us-east-1" },
    { key: "bucket", label: "Bucket", required: true },
    { key: "prefix", label: "Prefix", required: false },
    { key: "access_key_ref", label: "Access key reference", required: true },
    { key: "secret_key_ref", label: "Secret key reference", required: true },
  ],
  sftp: [
    { key: "host", label: "Host", required: true },
    { key: "port", label: "Port", required: false, hint: "22" },
    { key: "username", label: "Username", required: true },
    { key: "host_key_sha256", label: "Host key fingerprint", required: true, hint: "SHA256:…" },
    { key: "key_ref", label: "Private key reference", required: true },
    { key: "directory", label: "Directory", required: false },
  ],
  ftp: [
    { key: "host", label: "Host", required: true },
    { key: "port", label: "Port", required: false, hint: "21" },
    { key: "username", label: "Username", required: true },
    { key: "password_ref", label: "Password reference", required: true },
    { key: "directory", label: "Directory", required: false },
  ],
};

/**
 * Build the API body from the form, or the first problem.
 *
 * Empty optional fields are left out so the server defaults apply; `port` becomes a number and
 * FTP uses TLS unless `ftpPlain` is set.
 */
export function buildTarget(
  name: string,
  passphraseRef: string,
  keep: string,
  kind: Kind,
  values: Record<string, string>,
  ftpPlain = false,
): { body: BackupTarget } | { error: string } {
  if (!name.trim() || !passphraseRef.trim())
    return { error: t("Name and passphrase reference are required.") };
  const n = Number(keep);
  if (!Number.isInteger(n) || n < 1 || n > 1000)
    return { error: t("Keep must be a whole number from 1 to 1000.") };
  const target: Record<string, unknown> = { kind };
  for (const f of FIELDS[kind]) {
    const v = (values[f.key] ?? "").trim();
    if (!v) {
      if (f.required) return { error: t("{field} is required.", { field: t(f.label) }) };
      continue;
    }
    if (f.key === "port") {
      const p = Number(v);
      if (!Number.isInteger(p) || p < 1 || p > 65535)
        return { error: t("Port must be from 1 to 65535.") };
      target[f.key] = p;
    } else target[f.key] = v;
  }
  if (kind === "ftp") target.tls = !ftpPlain;
  return {
    body: {
      name: name.trim(),
      passphrase_ref: passphraseRef.trim(),
      keep: n,
      target,
    } as BackupTarget,
  };
}

function AddForm({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [pass, setPass] = useState("");
  const [keep, setKeep] = useState("14");
  const [kind, setKind] = useState<Kind>("local");
  const [values, setValues] = useState<Record<string, string>>({});
  const [plain, setPlain] = useState(false);
  const [problem, setProblem] = useState("");
  const create = useMutation({
    mutationFn: async (body: BackupTarget) =>
      unwrap(await api.POST("/api/v1/system/backup-targets", { body })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "backup-targets"] });
      void qc.invalidateQueries({ queryKey: ["admin", "upgrade-check"] });
      onDone();
    },
  });
  return (
    <form
      className="space-y-3 border-t p-4 text-sm"
      onSubmit={(e) => {
        e.preventDefault();
        const r = buildTarget(name, pass, keep, kind, values, plain);
        if ("error" in r) return setProblem(r.error);
        setProblem("");
        create.mutate(r.body);
      }}
    >
      <p role="note" className="text-muted-foreground">
        {t(
          "Enter OpenBao secret references (for example secret/backup/passphrase), never the secret values themselves. The controller reads them when it backs up. Keep the passphrase somewhere other than the backup: without it an archive cannot be opened.",
        )}
      </p>
      {create.error && <ErrorState error={create.error} />}
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block space-y-1">
          <span className="font-medium">{t("Name")}</span>
          <input className={INPUT} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="block space-y-1">
          <span className="font-medium">{t("Passphrase reference")}</span>
          <input className={INPUT} value={pass} onChange={(e) => setPass(e.target.value)} />
        </label>
        <label className="block space-y-1">
          <span className="font-medium">{t("Archives to keep")}</span>
          <input
            className={INPUT}
            inputMode="numeric"
            value={keep}
            onChange={(e) => setKeep(e.target.value)}
          />
        </label>
        <label className="block space-y-1">
          <span className="font-medium">{t("Kind")}</span>
          <select
            className={INPUT}
            value={kind}
            onChange={(e) => {
              setKind(e.target.value as Kind);
              setValues({});
            }}
          >
            <option value="local">local</option>
            <option value="s3">s3</option>
            <option value="sftp">sftp</option>
            <option value="ftp">ftp</option>
          </select>
        </label>
        {FIELDS[kind].map((f) => (
          <label key={`${kind}-${f.key}`} className="block space-y-1">
            <span className="font-medium">{t(f.label)}</span>
            <input
              className={INPUT}
              placeholder={f.hint}
              value={values[f.key] ?? ""}
              onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
            />
          </label>
        ))}
      </div>
      {kind === "ftp" && (
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={plain} onChange={(e) => setPlain(e.target.checked)} />
          <span>{t("Use plain FTP (the password crosses the network in clear)")}</span>
        </label>
      )}
      {problem && (
        <p role="alert" className="text-status-down">
          {problem}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={create.isPending}>
          {create.isPending ? t("Adding…") : t("Add target")}
        </Button>
        <Button type="button" size="sm" variant="outline" onClick={onDone}>
          {t("Cancel")}
        </Button>
      </div>
    </form>
  );
}

function describe(x: BackupTarget): string {
  const g = x.target as Record<string, unknown>;
  switch (x.target.kind) {
    case "local":
      return `local ${String(g.path || "/")}`;
    case "s3":
      return `s3 ${String(g.bucket)}`;
    default:
      return `${x.target.kind} ${String(g.host)}`;
  }
}

/** The list of targets with Test and Delete, and the Add target form. Anchor: `#backup-targets`. */
export function BackupTargets() {
  const qc = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, { ok: boolean; text: string }>>({});
  const targets = useQuery({
    queryKey: ["admin", "backup-targets"],
    queryFn: async () => unwrap(await api.GET("/api/v1/system/backup-targets")),
  });
  const test = useMutation({
    mutationFn: async (name: string) =>
      unwrap(
        await api.POST("/api/v1/system/backup-targets/{name}/test", { params: { path: { name } } }),
      ),
    onSuccess: (r, name) =>
      setResults((p) => ({
        ...p,
        [name]: {
          ok: true,
          text: t("Works. {n} archives found.", { n: String(r.archives.length) }),
        },
      })),
    onError: (e, name) =>
      setResults((p) => ({
        ...p,
        [name]: { ok: false, text: `${t("Failed")}: ${(e as Error).message}` },
      })),
  });
  const del = useMutation({
    mutationFn: async (name: string) =>
      unwrap(
        await api.DELETE("/api/v1/system/backup-targets/{name}", { params: { path: { name } } }),
      ),
    onSuccess: () => {
      setConfirm(null);
      void qc.invalidateQueries({ queryKey: ["admin", "backup-targets"] });
      void qc.invalidateQueries({ queryKey: ["admin", "upgrade-check"] });
    },
  });
  const list = targets.data ?? [];
  return (
    <div id="backup-targets" tabIndex={-1} className="border-b outline-none">
      <div className="flex items-center gap-3 px-4 py-3">
        <h3 className="text-sm font-semibold">{t("Backup targets")}</h3>
        {!adding && (
          <Button size="sm" variant="outline" className="ml-auto" onClick={() => setAdding(true)}>
            {t("Add target")}
          </Button>
        )}
      </div>
      {targets.isError && <ErrorState error={targets.error} />}
      {del.error && <ErrorState error={del.error} />}
      {targets.data && list.length === 0 && (
        <p className="px-4 pb-3 text-sm">
          {t("No backup target yet. Add one: local disk, S3-compatible storage, SFTP or FTP.")}
        </p>
      )}
      {list.length > 0 && (
        <ul className="divide-y border-t">
          {list.map((x) => (
            <li key={x.name} className="flex flex-wrap items-center gap-3 px-4 py-2 text-sm">
              <span className="font-medium">{x.name}</span>
              <span className="text-muted-foreground">
                {describe(x)}, {t("keep {n}", { n: String(x.keep) })}
              </span>
              {results[x.name] && (
                <StatusPill status={results[x.name]?.ok ? "healthy" : "down"}>
                  {results[x.name]?.text}
                </StatusPill>
              )}
              <span className="ml-auto flex gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  aria-label={t("Test {name}", { name: x.name })}
                  disabled={test.isPending}
                  onClick={() => test.mutate(x.name)}
                >
                  {t("Test")}
                </Button>
                {confirm === x.name ? (
                  <>
                    <Button
                      size="sm"
                      variant="destructive"
                      aria-label={t("Confirm delete {name}", { name: x.name })}
                      disabled={del.isPending}
                      onClick={() => del.mutate(x.name)}
                    >
                      {t("Delete target")}
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => setConfirm(null)}>
                      {t("Cancel")}
                    </Button>
                  </>
                ) : (
                  <Button
                    size="sm"
                    variant="outline"
                    aria-label={t("Delete {name}", { name: x.name })}
                    onClick={() => setConfirm(x.name)}
                  >
                    {t("Delete")}
                  </Button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      {confirm && (
        <p className="px-4 py-2 text-xs text-muted-foreground">
          {t("Archives already written stay where they are.")}
        </p>
      )}
      {adding && <AddForm onDone={() => setAdding(false)} />}
    </div>
  );
}
