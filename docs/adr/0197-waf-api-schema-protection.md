# 0197. API schema protection in the WAF policy

Status: Accepted
Date: 2026-10-12

## Context

Plan item D5 asks for API schema protection in the WAF. The Coraza and CRS WAF ([0140](0140-waf-policy-set.md)) is a *negative*
model: it knows attack patterns. An API has a *positive* model, its description: these methods on these paths, these parameters
with these types, these body types and sizes. A request outside it is wrong whether or not it looks like an attack, and parameter
tampering (a string where an integer is expected, a repeated parameter, a path with dot segments) is what a pattern list misses.
HAProxy can enforce most of that itself, before the request reaches the WAF or the origin.

## Decision

**A WAF policy may carry `api`.** An `api` object holds `mode` (`detect` or `block`, default `detect`, like the WAF itself),
`operations` (1 to 500), `base_path` (a prefix such as `/v1`, up to four segments), `allow_unknown_query` and `max_body_bytes`
(the default for operations that set none). An operation is a `method`, a `path` template (`/pets/{id}`: literal segments of
`A-Za-z0-9._~-` or `{name}`), the types of its path parameters, its `query` parameters (`name`, `kind` of `string`, `integer`,
`number`, `boolean`, `uuid` or `enum`, `required`, `values`, `max_length`), the `content_types` a body may have and
`max_body_bytes`. Method and template are unique. No free-form text reaches the configuration: every name is validated and
escaped, every pattern is built from fixed pieces and single-quoted.

**Checks, in the service's frontend before the WAF** (`render/apischema.py`):

1. *Path hygiene.* A path with `.` or `..` segments, `//`, or an encoded dot, slash or backslash is a violation. This replaces
   normalization (`normalize-uri` is still experimental in HAProxy 3.2), and it means the path that was checked is the path the
   origin sees.
2. *Operation.* The method and the whole path must match an operation (more literal segments are tried first, so `/orders/mine`
   wins over `/orders/{ref}`). A path that matches with another method is `405`, no match is `404`.
3. *Query string.* One regular expression covers the whole string and every `&`-separated pair, so a repeated parameter cannot
   hide a bad value behind a good first one. Unknown parameters, wrong types, values outside an enum and too-long strings are `400`.
   Required parameters are checked separately. With `allow_unknown_query` only the listed parameters are typed, and only their
   first occurrence is (HAProxy cannot iterate); the default refuses unknown parameters.
4. *Body.* A body with a media type outside `content_types` is `415`; a `Content-Length` over the limit is `413`; when a limit is set
   a chunked body is `411`, because its size cannot be known up front.

The first violation of a request is stored in `txn.sy_apiv` (`path`, `method`, `query`, `param`, `length`, `size`, `ctype`) and is
**captured into the access log line in both modes**. In `detect` mode nothing else happens. In `block` mode the status above is
answered (HAProxy has no built-in page for 411, 413 and 415, so those are plain-text responses).

**The importer.** `syctl waf api-schema openapi.yaml` (and `switchyard.importers.openapi`) reads OpenAPI 3.x or Swagger 2.0 and
prints the `api` object: base path from `servers` or `basePath`, path and query parameter types (an array is typed by its items),
required flags, request body media types. Header and cookie parameters, response schemas, JSON body schemas and security are
ignored. What cannot be represented (a path with characters outside the allowed set, a parameter name that is not a token, a
remote `$ref`, more than 500 operations) is reported and left out, so it is refused once the schema blocks.

**Defaults** (chosen without asking): detect first; unknown query parameters refused; no body limit unless set; the WAF's own
`mode` and the schema's `mode` are independent, so a service can block the schema and only detect CRS matches, or the reverse.

## Consequences

- The enforcement is HAProxy only; nothing new runs on the engine and no agent change is needed. Rules grow with the schema
  (about five per operation plus one per query rule); 500 operations render a few thousand lines, which the Data Plane API and
  `haproxy -c` handle, but a very large schema adds per-request work. Split large APIs across services.
- Tested three ways: golden file `27-waf-api-schema` (checked by `haproxy -c`), unit tests of the regular expressions, and
  `tests/integration/test_api_schema_runtime.py`, which sends about 30 requests to a real HAProxy in both modes and checks the status of each.
- A tenant that starts in `block` mode with an incomplete schema blocks real traffic. The importer and the detect default are the
  guard: read the `apiv` captures in the access log (the `{...}` block before the request line) before switching.

## Not built

Validation of JSON request bodies against their schema (HAProxy cannot parse JSON bodies, and Coraza has no JSON Schema
validator), response validation, header and cookie parameters, `oneOf` / `allOf` path variants, per-operation authentication
requirements, and a UI editor (the API and `syctl` are the way for now).

## Alternatives considered

- **Coraza rules generated from the schema.** Possible for parameters, but a second language to escape and the WAF sidecar becomes
  required for a feature that does not need it.
- **`normalize-uri` actions.** They would make dot segments harmless instead of refusing them, but the directive is experimental in 3.2.
- **Store the OpenAPI document itself.** It would put unvalidated, mostly irrelevant text (and remote references) into the
  desired state; the importer is a one-time translation the tenant can read and edit.
