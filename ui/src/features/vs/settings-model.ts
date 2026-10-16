// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/** Editable shape of a virtual service and the patch between two states. */
import type { components } from "@/api/schema";

export type VsRead = components["schemas"]["VirtualServiceRead"];
export type VsPatch = components["schemas"]["VirtualServicePatch"];

export const EDITABLE = [
  "name",
  "enabled",
  "se_group_id",
  "vip",
  "vip_pool_id",
  "ports",
  "certificate_ids",
  "default_pool_id",
  "routes",
  "policy_set_ids",
  "allow_list_id",
  "enforce_allow_list",
  "deny_list_id",
  "restore_client_ip_header",
  "compression",
  "cache",
  "labels",
] as const satisfies readonly (keyof VsRead & keyof VsPatch)[];

export type Editable = Pick<VsRead, (typeof EDITABLE)[number]>;

export function toEditable(v: VsRead): Editable {
  return Object.fromEntries(EDITABLE.map((k) => [k, structuredClone(v[k])])) as Editable;
}

/** Only the fields that differ from `base` (an update op carries changed fields only). */
export function buildPatch(base: Editable, next: Editable): VsPatch {
  const patch: Record<string, unknown> = {};
  for (const k of EDITABLE) {
    if (JSON.stringify(base[k]) !== JSON.stringify(next[k])) patch[k] = next[k];
  }
  return patch as VsPatch;
}

/** Field-level problems keyed by the first path segment, for the field's own message. */
export function errorsByField(
  errors: { path: string; message: string }[] | null | undefined,
): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const e of errors ?? []) (out[e.path] ??= []).push(e.message);
  return out;
}

/** Messages for `path` and everything under it (`routes` also matches `routes.0.pool_id`). */
export function messagesFor(errs: Record<string, string[]>, path: string): string[] {
  return Object.entries(errs)
    .filter(([p]) => p === path || p.startsWith(`${path}.`))
    .flatMap(([, m]) => m);
}
