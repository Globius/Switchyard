/**
 * Monarch syntax definitions for the config views: HAProxy configuration, and the unified diffs
 * the change-set diff endpoint returns. They are plain data so they can be tested without Monaco.
 */

/** Section headers that open a block in haproxy.cfg. */
export const HAPROXY_SECTIONS = [
  "global",
  "defaults",
  "frontend",
  "backend",
  "listen",
  "resolvers",
  "peers",
  "userlist",
  "program",
  "cache",
  "mailers",
  "http-errors",
  "ring",
];

/** Directives that are worth colouring (not the whole language; the rest reads as plain text). */
export const HAPROXY_KEYWORDS = [
  "bind",
  "mode",
  "log",
  "option",
  "no",
  "timeout",
  "balance",
  "server",
  "default-server",
  "default_backend",
  "use_backend",
  "acl",
  "http-request",
  "http-response",
  "tcp-request",
  "tcp-response",
  "redirect",
  "maxconn",
  "stats",
  "cookie",
  "stick-table",
  "stick",
  "http-check",
  "tcp-check",
  "capture",
  "compression",
  "errorfile",
  "filter",
  "nbthread",
  "daemon",
  "user",
  "group",
  "chroot",
  "ssl-default-bind-options",
  "ssl-default-bind-ciphers",
  "crt",
  "crt-list",
  "alpn",
  "check",
  "inter",
  "rise",
  "fall",
  "weight",
  "send-proxy",
  "send-proxy-v2",
  "accept-proxy",
  "if",
  "unless",
];

export const haproxyLanguage = {
  defaultToken: "",
  sections: HAPROXY_SECTIONS,
  keywords: HAPROXY_KEYWORDS,
  tokenizer: {
    root: [
      [/^\s*#.*$/, "comment"],
      [/\s#.*$/, "comment"],
      [
        /^(global|defaults|frontend|backend|listen|resolvers|peers|userlist|program|cache|mailers|http-errors|ring)\b/,
        "type.identifier",
      ],
      [/"([^"\\]|\\.)*"/, "string"],
      [/\b\d{1,3}(\.\d{1,3}){3}(\/\d+)?(:\d+)?\b/, "number.float"],
      [/\b\d+(ms|s|m|h|d)?\b/, "number"],
      [/[A-Za-z_][\w.-]*/, { cases: { "@keywords": "keyword", "@default": "" } }],
    ],
  },
};

/** Unified diff lines: additions, removals and hunk headers get their own token. */
export const diffLanguage = {
  defaultToken: "",
  tokenizer: {
    root: [
      [/^@@.*$/, "meta"],
      [/^\+.*$/, "diff.add"],
      [/^-.*$/, "diff.del"],
      [/.*/, ""],
    ],
  },
};

/** Which token a line of the haproxy language would get first; used by tests. */
export function classifyHaproxyLine(line: string): "comment" | "section" | "directive" | "other" {
  const l = line.trim();
  if (l.startsWith("#")) return "comment";
  const word = l.split(/\s+/)[0] ?? "";
  if (HAPROXY_SECTIONS.includes(word)) return "section";
  if (HAPROXY_KEYWORDS.includes(word)) return "directive";
  return "other";
}
