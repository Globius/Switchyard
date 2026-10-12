// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * Who owns a draft. The API stores an author string: `user:<provider>:<subject>` for people,
 * `token:<name>` for API tokens and `user:local:<username>` for the break-glass administrator.
 */
export type Author =
  | { kind: "person"; label: string }
  | { kind: "token"; label: string }
  | { kind: "break_glass"; label: string };

export function parseAuthor(author: string): Author {
  if (author.startsWith("token:")) return { kind: "token", label: author.slice(6) };
  if (author.startsWith("user:local:")) return { kind: "break_glass", label: author.slice(11) };
  return { kind: "person", label: author };
}

/**
 * The name used in "Draft by <name>, HH:MM": the first name for people, the token name for
 * tokens and "break-glass" for the local administrator.
 *
 * @example
 * draftOwnerName({ name: "Miguel Rodriguez", auth: "session", break_glass: false }) // "Miguel"
 */
export function draftOwnerName(me: { name: string; auth?: string; break_glass?: boolean }): string {
  if (me.break_glass) return "break-glass";
  if (me.auth === "token") return me.name.replace(/^token:/, "");
  return me.name.trim().split(/\s+/)[0] || me.name;
}
