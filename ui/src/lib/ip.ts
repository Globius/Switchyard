// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * IPv4 and IPv6 helpers for forms and tables: validation, `host:port` formatting (IPv6 gets
 * brackets), and a numeric ordering that works across both families.
 *
 * @example
 * hostPort("2001:db8::1", 443); // "[2001:db8::1]:443"
 * hostPort("10.0.0.1", 80); // "10.0.0.1:80"
 * [ "10.0.0.10", "10.0.0.9", "2001:db8::1" ].sort(compareIp); // ["10.0.0.9", "10.0.0.10", "2001:db8::1"]
 */

const V4_PART = /^(0|[1-9]\d{0,2})$/;

/** True for dotted-quad IPv4 such as `10.40.12.6` (no leading zeros, each part 0 to 255). */
export function isIPv4(s: string): boolean {
  const parts = s.split(".");
  return parts.length === 4 && parts.every((p) => V4_PART.test(p) && Number(p) <= 255);
}

/**
 * True for IPv6 text: full or `::`-compressed, optionally ending in an IPv4 part
 * (`::ffff:10.0.0.1`). Zone ids (`%eth0`) and brackets are not accepted.
 */
export function isIPv6(s: string): boolean {
  if (!s.includes(":") || /[^0-9a-fA-F:.]/.test(s)) return false;
  const halves = s.split("::");
  if (halves.length > 2) return false;
  const groups = (h: string) => (h === "" ? [] : h.split(":"));
  const all = halves.flatMap(groups);
  let count = all.length;
  const last = all[all.length - 1];
  if (last?.includes(".")) {
    if (!isIPv4(last)) return false;
    count += 1; // the IPv4 tail is two groups
    all.pop();
  }
  if (!all.every((g) => /^[0-9a-fA-F]{1,4}$/.test(g))) return false;
  // A trailing dotted part is only valid as the very last group.
  if (
    s
      .split(":")
      .slice(0, -1)
      .some((g) => g.includes("."))
  )
    return false;
  return halves.length === 2 ? count <= 7 : count === 8;
}

/** True for an IPv4 or IPv6 address. */
export function isIp(s: string): boolean {
  return isIPv4(s) || isIPv6(s);
}

/** The prefix length problem of a `addr/len` string, or null: 0 to 32 for IPv4, 0 to 128 for IPv6. */
export function cidrProblem(s: string): "address" | "prefix" | null {
  const [addr, len, ...rest] = s.split("/");
  if (addr === undefined || rest.length > 0) return "address";
  const v4 = isIPv4(addr);
  if (!v4 && !isIPv6(addr)) return "address";
  if (len === undefined) return null;
  if (!/^\d{1,3}$/.test(len)) return "address";
  return Number(len) <= (v4 ? 32 : 128) ? null : "prefix";
}

/** `host:port` for display and diffs; IPv6 hosts are bracketed. A missing port gives just the host. */
export function hostPort(host: string, port?: number | string | null): string {
  const h = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
  return port ? `${h}:${port}` : host;
}

function toBigInt(s: string): { v6: boolean; n: bigint } | null {
  if (isIPv4(s)) return { v6: false, n: s.split(".").reduce((a, p) => (a << 8n) + BigInt(p), 0n) };
  if (!isIPv6(s)) return null;
  let text = s;
  const tail = text.slice(text.lastIndexOf(":") + 1);
  if (tail.includes(".")) {
    const p = tail.split(".").map(Number);
    const hi = (((p[0] ?? 0) << 8) | (p[1] ?? 0)).toString(16);
    const lo = (((p[2] ?? 0) << 8) | (p[3] ?? 0)).toString(16);
    text = `${text.slice(0, text.lastIndexOf(":") + 1)}${hi}:${lo}`;
  }
  const [head = "", rest] = text.split("::");
  const h = head === "" ? [] : head.split(":");
  const r = rest === undefined ? [] : rest === "" ? [] : rest.split(":");
  const fill = rest === undefined ? [] : Array<string>(8 - h.length - r.length).fill("0");
  const groups = [...h, ...fill, ...r];
  return { v6: true, n: groups.reduce((a, g) => (a << 16n) + BigInt(`0x${g}`), 0n) };
}

/** Sort order for addresses: IPv4 before IPv6, each numerically; anything else last, as text. */
export function compareIp(a: string, b: string): number {
  const x = toBigInt(a);
  const y = toBigInt(b);
  if (!x || !y) return x ? -1 : y ? 1 : a.localeCompare(b);
  if (x.v6 !== y.v6) return x.v6 ? 1 : -1;
  return x.n < y.n ? -1 : x.n > y.n ? 1 : 0;
}
