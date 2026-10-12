# 0102. Header and URL rewrite policy sets

Status: Accepted
Date: 2026-10-12

## Context

Plan item A2. Tenants need to add, set and remove request and response headers (security headers, tracing ids, hiding
`Server`) and to rewrite a path prefix (versioned or relocated APIs). The risk named in the plan is injection: a header
value reaches the config, and `%[...]` in HAProxy is an expression language.

## Decision

A policy set of `kind: rewrite` holds up to 64 `rules`. Each rule has a `phase` (`request`, the default, or `response`), a
`match` ([0101](0101-route-match-model.md); empty applies to every request) and one `action`:

| `action.type` | Fields | Phases | Rendered as |
| --- | --- | --- | --- |
| `set_header` | `name`, `value` or `variable` | both | `http-request|response set-header Name "<value>"` |
| `add_header` | `name`, `value` or `variable` | both | `... add-header Name "<value>"` |
| `remove_header` | `name` | both | `... del-header Name` |
| `replace_prefix` | `from_prefix`, `to_prefix` | request | `http-request replace-path` (see below) |

**Values.** `value` is a literal that passes `validate_header_value` (decision of 0101). `variable` is one of a fixed list
and is the only way to put a per-request value into a header:

| `variable` | HAProxy fetch | `variable` | HAProxy fetch |
| --- | --- | --- | --- |
| `client_ip` | `src` | `tls_version` | `ssl_fc_protocol` |
| `client_port` | `src_port` | `tls_cipher` | `ssl_fc_cipher` |
| `host` | `var(txn.host)` (lower-cased, no port) | `tls_sni` | `ssl_fc_sni` |
| `vip` | `dst` | `method` | `method` |
| `vip_port` | `dst_port` | `request_id` | `uuid` (a new UUID per use) |

TLS variables are empty on a port without TLS. A literal never contains `%`, so a user cannot write a fetch; the renderer
re-checks the character set and quotes the value.

**Headers a policy cannot touch.** `Content-Length`, `Transfer-Encoding`, `Connection`, `Keep-Alive`, `TE`, `Trailer`,
`Upgrade`, `Expect` and the `Proxy-*` headers (message framing and hop-by-hop; changing them breaks or smuggles requests)
in both phases, and `X-Forwarded-For` and `X-Forwarded-Proto` on requests (the renderer owns client-address hygiene,
`docs/render.md`). `Host` can be set but not added or removed. Identity headers for the auth service (E3) will get their own
protection in their ADR.

**`replace_prefix`.** Works on whole path segments: `/api` to `/v2` turns `/api` into `/v2` and `/api/x` into `/v2/x` and
leaves `/apix` alone. `to_prefix: "/"` strips the prefix (`/api/x` becomes `/x`, `/api` becomes `/`). Neither prefix may end
in `/` (except the target `/`), and `from_prefix` is not `/`. The query string is kept. The renderer builds the regex from the
validated characters (`.` and `+` become `[.]` and `[+]`), single-quotes it and the replacement (nothing inside is expanded)
and doubles `%` in the replacement. The strip form emits a `set-path /` rule for the bare prefix first, then the regex rule.

**Order.** Rules run in list order inside a set and sets in attachment order. Request rules run after redirects and fixed
responses and before routing, so routing sees the rewritten path and headers (stage 5 of 0100); response rules run last.
A later rule sees the effect of an earlier one (a prefix replaced by rule 1 is the path rule 2 matches).

**Response rules and matches.** A response rule with a non-empty match is rendered as a request-stage flag
(`http-request set-var(txn.sy_mN) bool(true) if <match>`) and a response-stage test of the flag, because HAProxy has no
request fetches in `http-response` (0100, decision 5). Matches that use only the connection need no flag but get one for
uniformity.

## Consequences

- No `regex`, `%[...]`, `lf-string`, `set-var` or `sample`-converter surface for users.
- `Set-Cookie` and `Server` can be changed or removed on responses; a policy can therefore hide a backend's headers.
- A frontend with many rules grows linearly; the 64-rule cap keeps the config and the diff readable.
- UI: a form with rule rows (phase, match, action), the variable picker, and the list of protected headers; the API
  validator returns `cannot be changed by a policy` with the path of the rule.

## Alternatives considered

- **Free `http-request set-header` with log-format text.** One field would cover everything and is exactly the injection
  surface CLAUDE.md prohibits.
- **`replace-path` with a user regex.** Needs a regex validator and a ReDoS review for a case that prefixes cover.
- **Rewrite in the response phase through `capture`.** More moving parts than a flag.
