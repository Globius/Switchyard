import { useState } from "react";
import { ErrorState, TableSkeleton } from "@/components/sy/states";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";
import { show } from "@/lib/snapshot-diff";
import { cn } from "@/lib/utils";
import {
  valueFor,
  useConflictData,
  useResolveConflict,
  type Choice,
  type Conflict,
} from "./conflict";
import type { ChangeSet } from "./model";

const LABELS: Record<Choice, string> = {
  base: "Base",
  theirs: "Theirs",
  yours: "Yours",
  both: "Both",
};

/**
 * Three-way view for a stale apply: per conflicting field you pick Base, Theirs or Yours (and
 * Both, theirs first, when each side only added list items) before the draft is rewritten.
 */
export function ConflictResolver({
  tenant,
  cs,
  conflict,
  changeSet,
  onResolved,
}: {
  tenant: string;
  cs: string;
  conflict: Conflict;
  changeSet: ChangeSet;
  onResolved: () => void;
}) {
  const data = useConflictData(tenant, cs, changeSet.base_revision, conflict);
  const resolve = useResolveConflict(tenant, cs);
  const [picked, setPicked] = useState<Record<string, Choice>>({});

  if (data.isPending) return <TableSkeleton rows={3} cols={3} />;
  if (data.isError) return <ErrorState error={data.error} onRetry={() => void data.refetch()} />;
  const { conflicts, yoursOnly } = data.data;
  const complete = conflicts.every((c) => picked[c.field]);
  const name = String(data.data.yours.name ?? conflict.object_id);
  const opIndexes = changeSet.ops
    .map((o, i) => (o.object_id === conflict.object_id ? i : -1))
    .filter((i) => i >= 0);

  return (
    <section
      role="region"
      aria-label={t("Resolve conflicts")}
      className="space-y-4 rounded-lg border border-status-degraded/40 bg-card p-5"
    >
      <div>
        <h2 className="text-base font-semibold">
          {t("Resolve {n} conflicts", { n: conflicts.length })}
        </h2>
        <p className="text-sm text-muted-foreground">
          {t(
            "Someone applied a change that also changed {name} (version {from} is now {to}). Pick a value for each field, then apply.",
            {
              name,
              from: conflict.expected_version,
              to: conflict.current_version,
            },
          )}
        </p>
      </div>
      {conflicts.length === 0 && (
        <p className="text-sm text-muted-foreground">
          {t("No single field conflicts. Your changes can be kept on top of theirs.")}
        </p>
      )}
      {conflicts.map((c) => {
        const choices = (
          ["base", "theirs", "yours", ...(c.both ? ["both"] : [])] as Choice[]
        ).filter(Boolean);
        return (
          <fieldset key={c.field} className="space-y-2">
            <legend className="font-mono text-sm font-medium">{c.field}</legend>
            <div className="grid gap-2 md:grid-cols-4">
              {choices.map((ch) => (
                <label
                  key={ch}
                  className={cn(
                    "flex cursor-pointer gap-2 rounded-md border p-3 text-xs",
                    picked[c.field] === ch && "border-primary bg-accent",
                  )}
                >
                  <input
                    type="radio"
                    name={c.field}
                    aria-label={`${c.field}: ${t(LABELS[ch])}`}
                    checked={picked[c.field] === ch}
                    onChange={() => setPicked({ ...picked, [c.field]: ch })}
                  />
                  <span className="min-w-0">
                    <span className="block font-sans font-medium">
                      {t(LABELS[ch])}
                      {ch === "base" && ` #${changeSet.base_revision}`}
                    </span>
                    <span className="block font-mono break-words">{show(valueFor(c, ch))}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
        );
      })}
      {resolve.isError && <ErrorState error={resolve.error} />}
      <Button
        disabled={!complete || resolve.isPending}
        onClick={() =>
          resolve.mutate(
            {
              conflict,
              opIndexes,
              yoursOnly,
              choices: Object.fromEntries(
                conflicts.map((c) => [
                  c.field,
                  { choice: picked[c.field]!, value: valueFor(c, picked[c.field]!) },
                ]),
              ),
            },
            { onSuccess: onResolved },
          )
        }
      >
        {t("Use choices and re-review")}
      </Button>
    </section>
  );
}
