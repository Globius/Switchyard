# 0103. Fixed responses and custom error pages

Status: Accepted
Date: 2026-10-12

## Context

Plan item A4. Tenants want the load balancer to answer some requests itself (a health probe path, a blocked path, a
maintenance page) and to replace HAProxy's plain 503, 502 and 504 pages with their own text. The body is text the tenant
uploads; it is not config.

## Decision

**`kind: respond`.** Up to 64 `rules`, each `{match, status, content_type?, body?, retry_after_s?}`:

- `match` is a `RouteMatch` ([0101](0101-route-match-model.md)); an empty match answers every request, which is how a
  maintenance page is built (attach the set when needed, detach it when done, or add a header condition that operators
  control).
- `status` is one of 200, 202, 204, 400, 401, 403, 404, 405, 410, 429, 451, 500, 502, 503, 504. Redirects stay a separate
  rule kind.
- `content_type` is `text/plain`, `text/html` or `application/json`; `body` and `content_type` go together; a 204 has no
  body; `body` is non-empty text of at most 16 KiB with no control characters except tab, CR and LF.
- `retry_after_s` (1 to 86400) adds `Retry-After` and is only valid with 429 and 503.
- Rendered as `http-request return status <n> [content-type <t> file <path>] [hdr Retry-After <s>] [if <match>]`, in
  stage 4 of [0100](0100-policy-model-and-request-stages.md): after redirects, before rewrites and routing. The first
  match answers; the rules of the attached sets apply in attachment order.

**`kind: error_pages`.** `pages[]`, one per status, for 400, 403, 408, 500, 502, 503 and 504, each
`{status, content_type, body}` (type defaults to `text/html`, body required). Rendered as
`http-error status <n> content-type <t> file <path>` in the frontend, so HAProxy uses the page when it generates that error
for a request to the service (the backend's own error pages win if a backend defines one; Switchyard defines none). Two
error-page sets on one service may not define the same status (`duplicate_error_page`).

**Bodies are files, not config.** Each body is written beside the allow lists, as `haproxy/lists/<policy>-r<rule number>.page`
(respond) or `haproxy/lists/<policy>-e<status>.page` (error pages), with a trailing newline, and referenced with `file`
(not `lf-file`), so HAProxy serves the bytes without evaluating anything inside them. This reuses the Data Plane API's one
general-storage directory, so the SE image and the agent need no change. The files are part of the render fingerprint and
of the per-object rendered view (the policy set's fragment includes its pages). Names come from policy-set names, which are
validated identifiers, never from body text.

## Consequences

- A `text/html` body is served as written from the tenant's own domain. The tenant owns that content; the controller does not
  sanitise it. The 16 KiB cap and the content-type list keep it to small pages.
- A respond rule with a `retry_after_s` of 30 answers `Retry-After: 30`.
- The page files of two services in one SE group share the directory; policy-set names are unique per tenant, as the allow-list
  file names already assume. The renderer fails loudly rather than overwriting if two different tenants' sets with the same
  name ever share an SE group (existing limitation of the lists, now also covering pages).
- UI: a rule list with a body editor and a status picker for `respond`; one editor per status for `error_pages`.

## Alternatives considered

- **`http-request return ... string`/`lf-string` with the body inline.** Puts user text in `haproxy.cfg` (escaping, size) and
  `lf-string` evaluates `%[...]`.
- **`errorfile`.** Needs a full HTTP response in the file; `http-error` takes the body and builds the response.
- **Attaching error pages to the pool or the SE group.** Errors are per request to a service; a service is where tenants
  expect to set them.
