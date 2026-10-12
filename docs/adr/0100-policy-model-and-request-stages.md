# 0100. Policy model: one condition type and fixed request stages

Status: Accepted
Date: 2026-10-12

## Context

Plan items A2 (header and URL rewrite), A3 (richer matches) and A4 (fixed responses) of
[08-competitive-parity-plan.md](../design/08-competitive-parity-plan.md) touch the same model, and later blocks (A5 rate
limits, C2 weighted routes, E1 to E3 auth) add more rules to it. Doing it piecemeal would grow the request-order list in
`docs/render.md` rule by rule. This record fixes the shape once. The three items are decided in [0101](0101-route-match-model.md),
[0102](0102-header-and-url-rewrite.md) and [0103](0103-fixed-responses-and-error-pages.md).

## Decision

1. **One condition type.** `RouteMatch` is the only way to say "this request". Routes, redirect rules, rewrite rules and
   response rules all hold one. It grows (method, headers, query, source networks, SNI) instead of each rule kind getting
   its own condition. Every condition given must hold (AND); there is no OR (use two rules) and no negation except
   `absent` on a header.
2. **Policy sets stay the unit that is shared and attached.** A virtual service lists `policy_set_ids` in attachment order.
   The `kind` union grows by three kinds: `rewrite`, `respond` and `error_pages` (next to `redirect` and `allow_list`).
   `allow_list` is still attached through `allow_list_id`, not `policy_set_ids`.
3. **Request stages are fixed by kind, not by attachment order across kinds.** A frontend runs these stages in order; inside
   a stage the sets apply in attachment order and the rules of a set in list order. First match wins in the stages that
   end the request (redirects and responses).

   | Stage | Source | Ends the request |
   | --- | --- | --- |
   | 1 Connection filter | allow list with `enforce_allow_list` | yes (reject) |
   | 2 Header hygiene | client address, `X-Forwarded-*`, host variable | no |
| 2b Rate limits | `rate_limit` sets ([0110](0110-rate-and-concurrency-limits.md)) | yes (deny, tarpit, drop) |
   | 3 Redirects | HTTPS redirect, `redirect` sets, redirect routes | yes |
   | 4 Fixed responses | `respond` sets | yes |
   | 5 Request rewrites | `rewrite` sets, request phase | no |
   | 5b Access | `access` sets ([0130](0130-auth-service-architecture.md)): decision, then login through the proxy | yes (to the proxy, 401, 403) |
| 6 Routing | path and conditional routes by `order`, host maps, default pool | no |
   | 7 Response rewrites | `rewrite` sets, response phase | no |

   `error_pages` is not a request stage: it renders `http-error` lines that apply when HAProxy generates the error. New
   rule kinds in later blocks (rate limits, auth) take a numbered place in this table through their own ADR instead of
   being appended to the end.
4. **Everything a user supplies passes a strict validator and the renderer's own check** (CLAUDE.md). No free-form
   expressions: header values are literals from a restricted character set or one of ten named variables; there is no
   `%[...]`, no regex and no free snippet.
5. **A response rule that needs a request attribute remembers the match at request time.** HAProxy does not offer
   request fetches such as `path` in `http-response` rules, so a response rule with a non-empty match sets a
   per-transaction flag in stage 5 (`txn.sy_m<n>`) and tests it in stage 7.

## Consequences

- Services without these sets render exactly as before (the existing goldens are unchanged).
- A route with any condition besides the host is rendered as a `use_backend ... if` line in `order`, not as a map entry
  (ADR 0101). Existing path routes are the same case and keep their order.
- Tables: `policy_set.kind` accepts the new kinds (their rules are JSON in `rules`; the pages of `error_pages` too) and
  `vs_route.match_extra` (JSON) holds the new match conditions (migration 0027).
- The API schema of `RouteMatch` gains optional fields, and `policy-sets` gains three `kind` values. Both are additive.
- Later blocks reuse the condition type (A5 rate limits, C2 weighted routes) and add a row to the stage table.

## Alternatives considered

- **Attachment order across kinds** (one ordered list of mixed rules). More flexible, but a redirect attached after a
  rewrite would see a rewritten path, and which one wins would depend on UI drag order. Fixed stages make the result
  predictable; two sets of the same kind still order freely.
- **An expression language** like Avi/NetScaler policy expressions. Powerful and the largest injection surface in the
  product; CLAUDE.md rules it out for phase 1.
- **Separate condition types per rule kind.** Duplicates validators and the UI form for each kind.
