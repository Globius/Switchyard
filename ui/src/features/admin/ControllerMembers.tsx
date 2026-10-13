/**
 * Admin > Controller > Cluster: the controller's own VMs and what Patroni says about each one
 * (ADR 0051). In the interim placement the third VM runs on ESXi: the platform does not manage it,
 * so it is shown as an external member, and it never becomes the primary.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api, unwrap } from "@/api/client";
import type { components } from "@/api/schema";
import { ConfirmDialog } from "@/components/sy/ConfirmDialog";
import { StatusPill } from "@/components/sy/StatusPill";
import { EmptyState, ErrorState, TableSkeleton } from "@/components/sy/states";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Sheet } from "@/components/ui/sheet";
import { Switch } from "@/components/ui/switch";
import { t } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export type Member = components["schemas"]["ControllerMemberRead"];
type Platform = Member["platform"];

const INPUT =
  "h-9 w-full rounded-md border border-input bg-card px-3 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60";

export const PLATFORM_LABEL: Record<Platform, string> = {
  proxmox: "Proxmox",
  esxi: "ESXi",
  other: "Other",
};

/** Bytes as a short text: `0 B`, `512 B`, `2.0 KiB`, `3.5 MiB`. */
export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

/** What Patroni's role means for the reader: who leads, who follows, who is missing. */
export function DatabaseRole({ member }: { member: Member }) {
  if (member.in_cluster === null) return <span className="text-muted-foreground">—</span>;
  if (member.in_cluster === false)
    return <StatusPill status="down">{t("Not in the cluster")}</StatusPill>;
  switch (member.status.database_role) {
    case "leader":
      return <StatusPill status="healthy">{t("Primary")}</StatusPill>;
    case "standby_leader":
      return <StatusPill status="healthy">{t("Standby leader")}</StatusPill>;
    case "sync_standby":
      return <StatusPill status="healthy">{t("Sync standby")}</StatusPill>;
    case "replica":
      return <StatusPill status="neutral">{t("Replica")}</StatusPill>;
    default:
      return (
        <StatusPill status="degraded">{member.status.database_role ?? t("Unknown")}</StatusPill>
      );
  }
}

const OK_STATES = new Set(["running", "streaming"]);

function useMembers() {
  return useQuery({
    queryKey: ["admin", "controller", "members"],
    refetchInterval: 15_000,
    queryFn: async () => unwrap(await api.GET("/api/v1/system/controller/members")),
  });
}

interface Form {
  name: string;
  address: string;
  platform: Platform;
  host: string;
  nofailover: boolean;
  patroni_url: string;
}

const EMPTY: Form = {
  name: "",
  address: "",
  platform: "proxmox",
  host: "",
  nofailover: false,
  patroni_url: "",
};

function MemberSheet({ member, onClose }: { member: Member | "new"; onClose: () => void }) {
  const qc = useQueryClient();
  const [f, setF] = useState<Form>(
    member === "new"
      ? EMPTY
      : {
          name: member.name,
          address: member.address,
          platform: member.platform,
          host: member.host ?? "",
          nofailover: member.nofailover,
          patroni_url: member.patroni_url ?? "",
        },
  );
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [problem, setProblem] = useState<unknown>(null);
  const set = (p: Partial<Form>) => setF({ ...f, ...p });
  const save = useMutation({
    mutationFn: async () => {
      const body = {
        address: f.address.trim(),
        platform: f.platform,
        host: f.host.trim() || null,
        nofailover: f.nofailover,
        patroni_url: f.patroni_url.trim() || null,
      };
      if (member === "new")
        return unwrap(
          await api.POST("/api/v1/system/controller/members", {
            body: { name: f.name.trim(), ...body },
          }),
        );
      return unwrap(
        await api.PATCH("/api/v1/system/controller/members/{controller_member}", {
          params: {
            path: { controller_member: member.id },
            header: { "if-match": String(member.version) },
          },
          body,
        }),
      );
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin", "controller", "members"] });
      onClose();
    },
    onError: (e) => {
      setErrors({});
      setProblem(null);
      if (e instanceof ApiError && e.status === 422) {
        const m: Record<string, string> = {};
        for (const x of e.problem?.errors ?? []) m[x.path] = x.message;
        setErrors(m);
      } else setProblem(e);
    },
  });
  const field = (name: keyof Form, label: string, hint?: string, disabled = false) => (
    <label className="block space-y-1.5 text-sm font-medium">
      {label}
      <input
        aria-label={label}
        className={INPUT}
        disabled={disabled}
        value={String(f[name])}
        onChange={(e) => set({ [name]: e.target.value } as Partial<Form>)}
      />
      {hint && <span className="block text-xs font-normal text-muted-foreground">{hint}</span>}
      {errors[name] && (
        <span role="alert" className="block text-xs font-normal text-status-down">
          {errors[name]}
        </span>
      )}
    </label>
  );
  return (
    <Sheet
      open
      centered
      onOpenChange={(o) => !o && onClose()}
      title={member === "new" ? t("Add a controller VM") : t("Edit {name}", { name: member.name })}
      footer={
        <>
          <Button variant="ghost" className="ml-auto" onClick={onClose}>
            {t("Cancel")}
          </Button>
          <Button
            disabled={save.isPending || !f.name.trim() || !f.address.trim()}
            onClick={() => save.mutate()}
          >
            {member === "new" ? t("Add") : t("Save")}
          </Button>
        </>
      }
    >
      {problem != null && <ErrorState error={problem} />}
      {field(
        "name",
        t("Name"),
        t("The Patroni member name (PATRONI_NAME). Roles are matched on it."),
        member !== "new",
      )}
      {field("address", t("Address"))}
      <label className="block space-y-1.5 text-sm font-medium">
        {t("Runs on")}
        <select
          aria-label={t("Runs on")}
          className={INPUT}
          value={f.platform}
          onChange={(e) => set({ platform: e.target.value as Platform })}
        >
          {(Object.keys(PLATFORM_LABEL) as Platform[]).map((p) => (
            <option key={p} value={p}>
              {PLATFORM_LABEL[p]}
            </option>
          ))}
        </select>
        {f.platform !== "proxmox" && (
          <span className="block text-xs font-normal text-muted-foreground">
            {t("The platform does not manage this VM, so it is shown as an external member.")}
          </span>
        )}
        {errors.platform && (
          <span role="alert" className="block text-xs font-normal text-status-down">
            {errors.platform}
          </span>
        )}
      </label>
      {field("host", t("Host"), t("The hypervisor host, for example pve2 or esxi-01. Optional."))}
      {field(
        "patroni_url",
        t("Patroni URL"),
        t("For example http://10.0.0.13:8008. The first member that answers gives the roles."),
      )}
      <div className="flex items-center gap-3 text-sm">
        <Switch
          aria-label={t("Never becomes the primary")}
          checked={f.nofailover}
          onCheckedChange={(on) => set({ nofailover: on })}
        />
        <span>{t("Never becomes the primary (nofailover)")}</span>
      </div>
    </Sheet>
  );
}

export function ControllerMembers() {
  const qc = useQueryClient();
  const list = useMembers();
  const [editing, setEditing] = useState<Member | "new" | null>(null);
  const [removing, setRemoving] = useState<Member | null>(null);
  const remove = useMutation({
    mutationFn: async (m: Member) =>
      unwrap(
        await api.DELETE("/api/v1/system/controller/members/{controller_member}", {
          params: {
            path: { controller_member: m.id },
            header: { "if-match": String(m.version) },
          },
        }),
      ),
    onSuccess: () => {
      setRemoving(null);
      void qc.invalidateQueries({ queryKey: ["admin", "controller", "members"] });
    },
  });
  const d = list.data;
  return (
    <section aria-label={t("Cluster members")} className="space-y-3">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">{t("Cluster members")}</h2>
          <p className="text-sm text-muted-foreground">
            {t(
              "The controller VMs and what Patroni reports. A member outside the platform, such as the interim one on ESXi, is external and never becomes the primary.",
            )}
          </p>
        </div>
        <Button variant="outline" onClick={() => setEditing("new")}>
          {t("Add a VM")}
        </Button>
      </div>

      {d && d.cluster_reachable === false && (
        <div role="alert" className="rounded-md border bg-status-degraded-bg px-4 py-2 text-sm">
          {t("Patroni did not answer, so roles and lag are not shown.")}{" "}
          <span className="font-mono text-xs">{d.cluster_error}</span>
        </div>
      )}
      {d && d.cluster_reachable === null && d.members.length > 0 && (
        <p className="text-sm text-muted-foreground">
          {t("No member has a Patroni URL, so roles and lag are not shown.")}
        </p>
      )}

      <div className="overflow-x-auto rounded-lg border bg-card">
        {list.isPending ? (
          <TableSkeleton rows={3} cols={6} />
        ) : list.isError ? (
          <ErrorState error={list.error} onRetry={() => void list.refetch()} />
        ) : d!.members.length === 0 ? (
          <EmptyState
            title={t("No controller VMs listed")}
            body={t(
              "List them here, or in the adoption file (controller_members), to see their roles.",
            )}
          />
        ) : (
          <table className="w-full text-[13px]">
            <thead className="border-b bg-muted/60 text-left text-xs text-muted-foreground">
              <tr>
                {["Name", "Runs on", "Address", "Database", "State", "Lag", "Failover", ""].map(
                  (h, i) => (
                    <th key={i} className="px-4 py-2.5 font-medium">
                      {h && t(h)}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody className="divide-y">
              {d!.members.map((m) => (
                <tr key={m.id}>
                  <td className="px-4 py-3">
                    <span className="flex items-center gap-2 font-medium">
                      {m.name}
                      {m.external && <Badge tone="draft">{t("External")}</Badge>}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {PLATFORM_LABEL[m.platform]}
                    {m.host && <span className="text-muted-foreground"> · {m.host}</span>}
                  </td>
                  <td className="px-4 py-3 font-mono">{m.address}</td>
                  <td className="px-4 py-3">
                    <DatabaseRole member={m} />
                  </td>
                  <td className="px-4 py-3">
                    {m.status.database_state ? (
                      <StatusPill
                        status={OK_STATES.has(m.status.database_state) ? "healthy" : "degraded"}
                      >
                        {m.status.database_state}
                      </StatusPill>
                    ) : (
                      "—"
                    )}
                  </td>
                  <td className="px-4 py-3 font-mono">
                    {m.status.lag_bytes == null ? "—" : bytes(m.status.lag_bytes)}
                  </td>
                  <td className="px-4 py-3">
                    {(m.status.nofailover ?? m.nofailover) ? (
                      <span>{t("Never primary")}</span>
                    ) : (
                      <span className="text-muted-foreground">{t("Can take over")}</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-right whitespace-nowrap">
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={t("Edit {name}", { name: m.name })}
                      onClick={() => setEditing(m)}
                    >
                      {t("Edit")}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className={cn("text-status-down")}
                      aria-label={t("Remove {name}", { name: m.name })}
                      onClick={() => setRemoving(m)}
                    >
                      {t("Remove")}
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {d?.leader && (
        <p className="text-xs text-muted-foreground">
          {t("Primary right now: {name}.", { name: d.leader })}
        </p>
      )}

      {editing && <MemberSheet member={editing} onClose={() => setEditing(null)} />}
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(o) => !o && setRemoving(null)}
        title={t("Remove {name} from the list?", { name: removing?.name ?? "" })}
        confirmLabel={t("Remove")}
        busy={remove.isPending}
        onConfirm={() => removing && remove.mutate(removing)}
      >
        <p>
          {t(
            "This only forgets the entry. The VM, etcd and Patroni are not touched; to retire a member, remove it from etcd and Patroni first.",
          )}
        </p>
        {remove.error && <ErrorState error={remove.error} />}
      </ConfirmDialog>
    </section>
  );
}
